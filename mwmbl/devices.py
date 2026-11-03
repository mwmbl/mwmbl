"""Recording the crawler devices a user submits results from, and how much each has submitted."""

from datetime import datetime, timedelta

from django.db.models import F, Q, Sum
from django.db.models.functions import TruncDate

from mwmbl.models import Device, DeviceSubmission

# Devices are cosmetic: they exist so a user can tell their own crawlers apart in the UI.
# Nothing depends on the list being complete, so a bound is cheap insurance against a
# client that varies the name it reports - whether by accident (a container with a random
# hostname per start) or deliberately - filling the table one row per submission.
MAX_DEVICES_PER_USER = 50

HOSTNAME_MAX_LENGTH = Device._meta.get_field("hostname").max_length

SUBMISSION_HISTORY_DAYS = 30


def record_device(user, hostname: str) -> Device:
    """Note that `user` is crawling from `hostname`, creating the device if it is new.

    The name is truncated rather than rejected. It arrives on the same request as the
    crawl results, Django does not validate field lengths on save(), and an over-long name
    would otherwise raise DataError before indexing and lose every page in the submission -
    a heavy price for a label.

    Over the cap, the least recently seen devices are dropped rather than the new one being
    refused, so the list always reflects what the user is actually crawling with now.
    """
    device, created = Device.objects.update_or_create(user=user, hostname=hostname[:HOSTNAME_MAX_LENGTH], defaults={})
    if not created:
        return device

    stale_ids = list(
        Device.objects.filter(user=user).order_by("-last_seen").values_list("id", flat=True)[MAX_DEVICES_PER_USER:]
    )
    if stale_ids:
        Device.objects.filter(id__in=stale_ids).delete()
    return device


def record_device_results(device: Device, num_results: int, now: datetime) -> None:
    Device.objects.filter(pk=device.pk).update(total_results=F("total_results") + num_results)
    DeviceSubmission.objects.create(device=device, submitted_at=now, num_results=num_results)
    # Pruned on write, so the table stays bounded without a scheduled job.
    cutoff = now - timedelta(days=SUBMISSION_HISTORY_DAYS)
    DeviceSubmission.objects.filter(device=device, submitted_at__lt=cutoff).delete()


def get_device_activity(device_ids: list[int], now: datetime) -> dict[int, dict]:
    """Results submitted in the last hour, the last 24 hours and on each of the last 30 UTC days,
    for each device. Days run oldest first and include those with nothing submitted."""
    today = now.date()
    days = [today - timedelta(days=offset) for offset in range(SUBMISSION_HISTORY_DAYS - 1, -1, -1)]
    submissions = DeviceSubmission.objects.filter(device_id__in=device_ids)

    windows = (
        submissions.filter(submitted_at__gte=now - timedelta(hours=24))
        .values("device_id")
        .annotate(
            last_hour=Sum("num_results", filter=Q(submitted_at__gte=now - timedelta(hours=1))),
            last_24h=Sum("num_results"),
        )
    )
    window_totals = {row["device_id"]: row for row in windows}

    per_day = (
        submissions.filter(submitted_at__date__gte=days[0])
        .annotate(day=TruncDate("submitted_at"))
        .values("device_id", "day")
        .annotate(results=Sum("num_results"))
    )
    daily_totals = {(row["device_id"], row["day"]): row["results"] for row in per_day}

    return {
        device_id: {
            "results_last_hour": window_totals.get(device_id, {}).get("last_hour") or 0,
            "results_last_24h": window_totals.get(device_id, {}).get("last_24h") or 0,
            "results_daily": [{"date": day, "results": daily_totals.get((device_id, day), 0)} for day in days],
        }
        for device_id in device_ids
    }
