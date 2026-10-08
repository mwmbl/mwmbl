"""Admin-only status pages: the blacklist filtering state in Redis, the weekly index count,
and paying users.

The blacklist page gives visibility on the blacklist filtering state that lives in Redis.

Retrieval filtering, the snapshot refresh and the index purge are three processes talking
to each other through Redis keys, and none of that state is reachable from the Django
admin: a snapshot that never publishes and a purge queue that never drains look exactly
like everything working, because retrieval quietly falls back to the built-in rules either
way. This puts the whole loop - the published snapshot, what this worker actually loaded,
the queue waiting to be purged, and the background tasks that maintain both - on one page.

The page is read-only, and deliberately cheap: the snapshot's size comes from STRLEN so a
page load never pulls the ~11 MB blob out of Redis, and the queue is sampled with
SRANDMEMBER so looking at it cannot consume it.
"""

import os
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from background_task.models import CompletedTask, Task
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import F, OuterRef, Q, Subquery
from django.shortcuts import render
from redis import RedisError

from mwmbl import count_urls, pricing, quota
from mwmbl.crawler.stats import BLACKLISTED_REMOVED_COUNT_KEY
from mwmbl.curated_domains import get_curated_domains
from mwmbl.indexer import blacklist_snapshot, purge_queue
from mwmbl.indexer.blacklist_snapshot import (
    HASH_DTYPE,
    SNAPSHOT_KEY,
    SNAPSHOT_VERSION_KEY,
    get_snapshot_blacklist,
)
from mwmbl.indexer.purge_queue import MAX_QUEUE_SIZE, PURGE_QUEUE_KEY, peek_purge_queue
from mwmbl.membership import TIERS, combined_search_monthly_limit
from mwmbl.models import ApiKey, Membership, MwmblUser
from mwmbl.utils import utc_today

logger = getLogger(__name__)


SNAPSHOT_TASK_NAME = "mwmbl.background.refresh_blacklist_snapshot"
PURGE_TASK_NAME = "mwmbl.background.purge_blacklisted_from_queue"
INDEX_COUNT_TASK_NAME = "mwmbl.background.count_index_urls"

QUEUE_SAMPLE_SIZE = 50
REMOVED_COUNT_DAYS = 14
PUBLISHED_INDEX_COUNT_DAYS = 60


def _snapshot_status() -> dict:
    """What is published, and what this worker is actually filtering against.

    The two can differ, and the difference is the interesting part. Each gunicorn worker
    loads the snapshot independently and only re-checks every
    BLACKLIST_SNAPSHOT_CHECK_SECONDS, so a freshly published snapshot legitimately shows
    as out of date here for a few minutes. A worker stuck on an old version for longer
    than that is a real problem.
    """
    client = blacklist_snapshot.get_redis()
    published_version = client.get(SNAPSHOT_VERSION_KEY)
    if published_version is not None:
        published_version = published_version.decode()

    # STRLEN, not GET: the blob is ~11 MB and this page is only reporting on its size.
    # Missing keys have length 0, which is also how the blob's absence is detected.
    blob_size = client.strlen(SNAPSHOT_KEY)

    snapshot = get_snapshot_blacklist()
    return {
        "published_version": published_version,
        "blob_present": blob_size > 0,
        "blob_size": blob_size,
        "blob_domains": blob_size // HASH_DTYPE.itemsize,
        # load_now() refuses a blob that is not a whole number of hashes, so surface the
        # same check rather than leaving a rejected snapshot looking healthy here.
        "blob_size_valid": blob_size % HASH_DTYPE.itemsize == 0,
        "loaded_version": snapshot.loaded_version,
        "loaded_domains": snapshot.num_domains,
        "up_to_date": snapshot.loaded_version == published_version,
        "check_seconds": settings.BLACKLIST_SNAPSHOT_CHECK_SECONDS,
        "refresh_seconds": settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS,
    }


def _queue_status() -> dict:
    client = purge_queue.get_redis()
    size = client.scard(PURGE_QUEUE_KEY)
    return {
        "size": size,
        "max_size": MAX_QUEUE_SIZE,
        "full": size >= MAX_QUEUE_SIZE,
        "batch_size": settings.BLACKLIST_PURGE_BATCH_SIZE,
        "interval_seconds": settings.BLACKLIST_PURGE_INTERVAL_SECONDS,
        "sample": peek_purge_queue(QUEUE_SAMPLE_SIZE, client),
    }


def _removed_counts(days: int) -> list[dict]:
    """Documents the purge has removed from the index, per day, most recent first.

    Retrieval filters blacklisted domains out of results whether or not the removal ever
    happens, so a queue that is filling while this stays at zero is the signature of a
    broken loop - see StatsManager.record_blacklisted_removed.
    """
    client = purge_queue.get_redis()
    today = utc_today()
    dates = [today - timedelta(days=i) for i in range(days)]
    counts = client.mget([BLACKLISTED_REMOVED_COUNT_KEY.format(date=date) for date in dates])
    return [{"date": date, "count": int(count) if count else 0} for date, count in zip(dates, counts)]


def _curated_status() -> dict:
    """Approved domains, and whether any of them are still being filtered out.

    Curated domains are subtracted from the remote lists when the snapshot is built, so
    "still blacklisted" is normally zero. Anything listed here is either waiting for the
    next rebuild, or blocked by a local rule curation does not override - EXCLUDED_DOMAINS
    or DOMAIN_BLACKLIST_REGEX - which is otherwise invisible.
    """
    curated_domains = get_curated_domains()
    still_blacklisted = get_snapshot_blacklist().filter_blacklisted(curated_domains)
    return {
        "count": len(curated_domains),
        "still_blacklisted": sorted(still_blacklisted),
        "approval_delay_seconds": settings.BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS,
    }


def _task_status(task_names: list[str]) -> list[dict]:
    """The background tasks that maintain the state a status page reports on.

    None of them runs without a `manage.py process_tasks` worker, and one running an image
    that predates a task skips it silently by name rather than failing - so "pending,
    run_at long past, never completed" is what a missing or stale worker looks like, and
    it is otherwise invisible.
    """
    pending = {task.task_name: task for task in Task.objects.filter(task_name__in=task_names)}

    statuses = []
    for task_name in task_names:
        last_completed = CompletedTask.objects.filter(task_name=task_name).order_by("-run_at").first()
        statuses.append(
            {
                "name": task_name,
                "task": pending.get(task_name),
                "last_completed": last_completed,
            }
        )
    return statuses


@staff_member_required
def blacklist_status_view(request):
    context = {
        "title": "Blacklist status",
        # This page reports on one gunicorn worker's in-memory snapshot, and the request
        # lands on whichever worker the load balancer picked. Named so the template can
        # say which one, because "loaded version" is otherwise ambiguous across workers.
        "worker_pid": os.getpid(),
    }

    # A status page for Redis state has to render when Redis is down - that is precisely
    # the failure it exists to report. Everything below this line comes from Redis, so one
    # handler covers the lot.
    try:
        context["snapshot"] = _snapshot_status()
        context["curated"] = _curated_status()
        context["queue"] = _queue_status()
        context["removed_counts"] = _removed_counts(REMOVED_COUNT_DAYS)
    except RedisError as e:
        logger.exception("Could not read blacklist status from Redis")
        context["redis_error"] = str(e)

    context["tasks"] = _task_status([SNAPSHOT_TASK_NAME, PURGE_TASK_NAME])
    return render(request, "admin/blacklist_status.html", context)


@staff_member_required
def index_count_view(request):
    context = {"title": "Index count"}
    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    # Rendered with Redis down, like the blacklist page: the scan stalling because Redis
    # is unreachable is one of the things this page is for.
    try:
        redis = count_urls.get_redis()
        context["scan"] = count_urls.get_scan_status(redis, index_path)
        context["published"] = count_urls.get_published_counts(redis, PUBLISHED_INDEX_COUNT_DAYS)
    except RedisError as e:
        logger.exception("Could not read the index count from Redis")
        context["redis_error"] = str(e)

    context["tasks"] = _task_status([INDEX_COUNT_TASK_NAME])
    return render(request, "admin/index_count.html", context)


def _api_users() -> list[dict]:
    """Users holding a search API key, plus anyone with a Polar usage subscription.

    Subscribers are included even without a key, because they are still being billed for
    the month's usage until it is reported. A subscriber at $0 has given card details but
    is still hard-capped at the free allowance.
    """
    last_key_use = (
        ApiKey.objects.filter(user=OuterRef("pk"), scopes__contains=[ApiKey.Scope.SEARCH])
        .order_by(F("last_used").desc(nulls_last=True))
        .values("last_used")[:1]
    )
    users = list(
        MwmblUser.objects.filter(
            Q(apikey__scopes__contains=[ApiKey.Scope.SEARCH]) | Q(billing__polar_subscription_id__gt="")
        )
        .distinct()
        .select_related("billing")
        .annotate(last_key_use=Subquery(last_key_use))
    )
    # One round trip for every counter rather than one per row.
    usage_keys = {user.id: quota._monthly_key(user.id) for user in users}
    usage_by_key = cache.get_many(list(usage_keys.values()))

    api_users = []
    for user in users:
        usage = usage_by_key.get(usage_keys[user.id], 0)
        billing = getattr(user, "billing", None)
        subscribed = billing is not None and billing.polar_subscription_id != ""
        spend_cents = billing.max_monthly_spend_cents if billing else 0
        if not subscribed:
            status = "no subscription"
        elif spend_cents == 0:
            status = "free"
        elif billing.cancel_at_period_end:
            status = "canceling"
        else:
            status = "active"
        api_users.append(
            {
                "user": user,
                "status": status,
                "subscribed": subscribed,
                "max_monthly_spend_cents": spend_cents,
                "monthly_cap": pricing.effective_monthly_request_cap(spend_cents),
                "usage": usage,
                "estimated_cost_cents": pricing.estimated_cost_cents(usage),
                "last_key_use": user.last_key_use,
                "current_period_end": billing.current_period_end if billing else None,
                "polar_customer_id": billing.polar_customer_id if billing else "",
            }
        )
    api_users.sort(key=lambda api_user: (api_user["estimated_cost_cents"], api_user["usage"]), reverse=True)
    return api_users


def _members() -> list[dict]:
    price_by_tier = {tier_info.tier.value: tier_info.monthly_price_pence for tier_info in TIERS}
    memberships = list(Membership.objects.select_related("user").order_by("-started"))
    usage_keys = {
        membership.user_id: quota._combined_search_monthly_key(membership.user_id) for membership in memberships
    }
    usage_by_key = cache.get_many(list(usage_keys.values()))

    return [
        {
            "user": membership.user,
            "tier": membership.get_tier_display(),
            "monthly_price_pence": price_by_tier[membership.tier],
            "combined_search_limit": combined_search_monthly_limit(membership.tier),
            "combined_search_usage": usage_by_key.get(usage_keys[membership.user_id], 0),
            "current_period_end": membership.current_period_end,
            "cancel_at_period_end": membership.cancel_at_period_end,
            "started": membership.started,
        }
        for membership in memberships
    ]


def _tier_summary(members: list[dict]) -> list[dict]:
    summary = []
    for tier_info in TIERS:
        tier_members = [member for member in members if member["tier"] == tier_info.tier.label]
        summary.append(
            {
                "tier": tier_info.tier.label,
                "count": len(tier_members),
                "monthly_price_pence": tier_info.monthly_price_pence,
                "monthly_revenue_pence": len(tier_members) * tier_info.monthly_price_pence,
            }
        )
    return summary


SEED_SEARCH_TOP_USERS = 25


def _seed_search_stats() -> dict:
    """This month's Seed Search (Combined Search) usage by signed-in users, by tier.

    Combined Search refuses API keys, so every counter belongs to a signed-in web user.
    "free" is everyone without a membership, matching COMBINED_SEARCH_MONTHLY_LIMITS.
    """
    counts = quota.get_all_monthly_combined_search_counts()
    users_by_id = MwmblUser.objects.in_bulk(list(counts))
    # Counters outlive deleted accounts until they expire, so skip users that no longer exist.
    counts = {user_id: count for user_id, count in counts.items() if user_id in users_by_id}
    tier_by_user = dict(Membership.objects.filter(user_id__in=counts).values_list("user_id", "tier"))

    tier_names = ["free"] + [tier_info.tier.value for tier_info in TIERS]
    by_tier = {tier: {"tier": tier, "users": 0, "queries": 0, "at_limit": 0} for tier in tier_names}
    rows = []
    for user_id, count in counts.items():
        tier = tier_by_user.get(user_id, "free")
        limit = combined_search_monthly_limit(tier)
        tier_stats = by_tier[tier]
        tier_stats["users"] += 1
        tier_stats["queries"] += count
        tier_stats["at_limit"] += count >= limit
        rows.append({"user": users_by_id[user_id], "tier": tier, "usage": count, "limit": limit})

    for tier_stats in by_tier.values():
        tier_stats["limit"] = combined_search_monthly_limit(tier_stats["tier"])

    rows.sort(key=lambda row: row["usage"], reverse=True)
    return {
        "users": len(counts),
        "queries": sum(counts.values()),
        "at_limit": sum(tier_stats["at_limit"] for tier_stats in by_tier.values()),
        "by_tier": list(by_tier.values()),
        "top_users": rows[:SEED_SEARCH_TOP_USERS],
    }


@staff_member_required
def paying_users_view(request):
    api_users = _api_users()
    members = _members()
    tier_summary = _tier_summary(members)
    # Members still paying out the current period after cancelling are counted: they have paid.
    context = {
        "title": "Paying users",
        "api_users": api_users,
        "api_subscribed_count": sum(1 for api_user in api_users if api_user["subscribed"]),
        "api_billed_count": sum(1 for api_user in api_users if api_user["estimated_cost_cents"] > 0),
        "api_estimated_revenue_cents": sum(api_user["estimated_cost_cents"] for api_user in api_users),
        "members": members,
        "tier_summary": tier_summary,
        "seed_search": _seed_search_stats(),
        "membership_revenue_pence": sum(tier["monthly_revenue_pence"] for tier in tier_summary),
        "free_keyed_monthly_limit": pricing.FREE_KEYED_MONTHLY_LIMIT,
        "price_per_1000_queries_cents": pricing.PRICE_PER_1000_QUERIES_CENTS,
    }
    return render(request, "admin/paying_users.html", context)
