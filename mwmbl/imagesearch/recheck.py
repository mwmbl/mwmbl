"""Rechecks stored Commons files against Commons as it is now (the recheck_images command).

A file is revoked when it is deleted or moved, nominated for deletion, denied or flagged by the rules. When its
content (the file's SHA-1 or the text the judge reads) has changed, its NSFW score came from another model or
question, or its revocation said nothing about its content, the judge is asked again and the file revoked unless
it still passes. Changed content must also have settled, as Seed Search requires, before it is approved.

A file revoked for a reason that holds for its content (gone, denied, nominated, rules, NSFW) stays revoked while
the content is unchanged, even if the denylist or the rules change; an operator restores one by deleting its row,
so that it is judged afresh the next time Seed Search finds it.
"""

from collections import Counter
from datetime import datetime, timezone
from logging import getLogger

import httpx
from django.conf import settings
from django.db import transaction

from mwmbl.imagesearch import commons, judge, rules, store
from mwmbl.imagesearch.prompt import NSFW_THRESHOLD
from mwmbl.imagesearch.seed_images import is_denied, is_nominated, is_settled
from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage, Verdict
from mwmbl.models import CommonsImage

logger = getLogger(__name__)
Reason = CommonsImage.RevocationReason
UPDATED_FIELDS = [field.name for field in CommonsImage._meta.concrete_fields if field.name not in ("id", "page_url")]


def revocation_reason(
    row: CommonsImage, files: dict[str, SourceImage | None], settled_before: datetime
) -> Reason | None:
    if row.page_url not in files:
        return Reason.GONE
    image = files[row.page_url]
    if image is None:
        return Reason.UNUSABLE
    # A renamed file answers under its new name, which gets a row of its own when Seed Search finds it.
    if commons.commons_file_key(image.page_url) != row.page_url:
        return Reason.GONE
    if is_denied(image):
        return Reason.DENIED
    if is_nominated(image):
        return Reason.NOMINATED
    if rules.image_verdict(image) != Verdict.SAFE:
        return Reason.RULES
    if store.is_changed(row, image) and not is_settled(image, settled_before):
        return Reason.UNSETTLED
    return None


def needs_judging(row: CommonsImage, image: SourceImage, fingerprint: str) -> bool:
    stale = row.nsfw_fingerprint != fingerprint
    return store.is_changed(row, image) or stale or row.revocation_reason in store.RECOVERABLE_REASONS


def judge_rows(to_judge: list[tuple[CommonsImage, SourceImage]], fingerprint: str, now: datetime, outcomes: Counter):
    try:
        nsfw_scores = judge.nsfw_scores([image for _, image in to_judge])
    except Exception as e:
        # Fails closed, and recoverably, so that a judge outage or one malformed answer never stops the recheck.
        logger.warning("Judge failed while rechecking images: %s", type(e).__name__)
        nsfw_scores = [None] * len(to_judge)
    for (row, image), score in zip(to_judge, nsfw_scores, strict=True):
        if score is None:
            store.revoke(row, Reason.UNJUDGED, now, image)
            outcomes[f"revoked: {Reason.UNJUDGED}"] += 1
        elif score >= NSFW_THRESHOLD:
            store.revoke(row, Reason.NSFW, now, image)
            outcomes[f"revoked: {Reason.NSFW}"] += 1
        elif store.may_approve(row, image):
            outcomes["restored" if row.revoked_at else "rescored"] += 1
            store.approve(row, image, score, fingerprint, now)
        else:
            row.last_checked_at = now
            outcomes["still revoked"] += 1


def recheck_batch(client: httpx.Client, page_urls: list[str], outcomes: Counter) -> None:
    """Recheck up to commons.FILES_PER_REQUEST files with one request to Commons and at most one to the judge."""
    files = commons.fetch_files(client, page_urls)
    now = datetime.now(timezone.utc)
    settled_before = now - settings.IMAGE_SEARCH_MIN_REVISION_AGE
    fingerprint = judge.nsfw_fingerprint()
    with transaction.atomic():
        # Locked and read afresh, so that a revocation made since the run began is never written over.
        locked = CommonsImage.objects.select_for_update().filter(page_url__in=page_urls)
        rows = list(locked.exclude(revocation_reason=Reason.OPERATOR))
        to_judge = []
        for row in rows:
            reason = revocation_reason(row, files, settled_before)
            image = files.get(row.page_url)
            if reason is not None:
                store.revoke(row, reason, now, image)
                outcomes[f"revoked: {reason}"] += 1
            elif needs_judging(row, image, fingerprint):
                to_judge.append((row, image))
            elif row.revoked_at is None:
                # Unchanged, so the score stands; licence, thumbnail and dates are brought up to date.
                store.approve(row, image, row.nsfw_score, fingerprint, now)
                outcomes["unchanged"] += 1
            else:
                row.last_checked_at = now
                outcomes["still revoked"] += 1
        if to_judge:
            judge_rows(to_judge, fingerprint, now, outcomes)
        CommonsImage.objects.bulk_update(rows, UPDATED_FIELDS)


def recheck(limit: int) -> Counter:
    """Recheck the `limit` files checked longest ago, a batch at a time, stopping with what is done if Commons asks
    us to back off, fails or refuses. Operator revocations are never lifted, so those files are not rechecked."""
    rows = CommonsImage.objects.exclude(revocation_reason=Reason.OPERATOR).order_by("last_checked_at", "id")
    page_urls = list(rows.values_list("page_url", flat=True)[:limit])
    outcomes = Counter()
    with commons.new_client() as client:
        for start in range(0, len(page_urls), commons.FILES_PER_REQUEST):
            batch = page_urls[start : start + commons.FILES_PER_REQUEST]
            try:
                recheck_batch(client, batch, outcomes)
            except ImageSourceUnavailable as e:
                # Commons is asked before anything changes, so the batch is left as it was, to go first next run.
                logger.warning("Stopped rechecking images: %s", e)
                outcomes["left unchecked"] += len(page_urls) - start
                break
    return outcomes
