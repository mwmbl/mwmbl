"""Keeping the errors a user's crawlers report, so the user can see them on their devices page."""

from mwmbl.crawler.batch import CrawlerErrorReport
from mwmbl.models import CrawlerError

# A crawler failing on every pass reports at most once an hour per distinct error, but
# anyone with a crawl key can send reports as fast as they like, so the history is bounded.
MAX_ERRORS_PER_USER = 200


def record_crawler_error(user, report: CrawlerErrorReport) -> None:
    """Store `report` for `user`, dropping their oldest errors beyond `MAX_ERRORS_PER_USER`."""
    innermost = report.frames[-1]
    location = f"{innermost.filename}:{innermost.lineno} in {innermost.function}"
    CrawlerError.objects.create(
        user=user,
        device_name=report.device_name,
        context=report.context,
        exception_type=report.exception_type,
        message=report.message,
        location=location,
        crawler_version=report.crawler_version,
    )

    stale_ids = list(
        CrawlerError.objects.filter(user=user)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)[MAX_ERRORS_PER_USER:]
    )
    if stale_ids:
        CrawlerError.objects.filter(id__in=stale_ids).delete()
