"""Signal receivers. Connected from MwmblConfig.ready().

Both of them are about a domain submission reaching work that happens elsewhere:

* Approving one has to reach the blacklist snapshot, because approved domains are subtracted
  from the remote lists when the snapshot is built rather than checked per query (see
  mwmbl.indexer.blacklist_snapshot). Without this an approval would sit inert for up to
  BLACKLIST_SNAPSHOT_REFRESH_SECONDS - six hours - and the moderator would reasonably
  conclude it had not worked.
* Creating one has to reach the moderation crawler, so that by the time a moderator opens the
  queue the domain has already been fetched and scored. Doing that work on their request
  instead would put three page fetches in front of every row.
"""
from datetime import timedelta
from logging import getLogger

from background_task.models import Task
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from mwmbl.models import DomainSubmission

logger = getLogger(__name__)


BLACKLIST_SNAPSHOT_TASK = "mwmbl.background.refresh_blacklist_snapshot"


@receiver(post_save, sender=DomainSubmission)
def enrich_new_submission(sender, instance: DomainSubmission, created: bool, **kwargs):
    """Crawl a newly submitted domain so the moderation queue has a suggestion waiting.

    Only on creation: a moderator saving a decision must not trigger a re-crawl. The task
    itself skips domains whose evidence is still fresh, so a resubmitted domain costs nothing.
    """
    if not created:
        return

    from mwmbl.background import enrich_domain_submission

    enrich_domain_submission(instance.name)
    logger.info("Scheduled moderation enrichment for %s", instance.name)


@receiver(post_save, sender=DomainSubmission)
def rebuild_blacklist_snapshot_on_approval(sender, instance: DomainSubmission, **kwargs):
    """Schedule a snapshot rebuild when a submission is approved.

    Debounced rather than immediate. Moderators work through the queue in batches, and each
    rebuild downloads and parses tens of megabytes, so the run is scheduled
    BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS ahead and only if nothing is already due by
    then. The check matches the repeating task's row too, so an approval shortly before the
    periodic rebuild adds nothing at all.

    post_save carries no previous value, so this fires on every save of an approved
    submission rather than only on the transition. The debounce absorbs that. Two approvals
    saved at once could both schedule a run, which is harmless: publish_snapshot versions the
    blob by content hash, so an identical snapshot does not make the workers re-read 11 MB.
    """
    if instance.status != "APPROVED":
        return

    from mwmbl.background import refresh_blacklist_snapshot

    delay = settings.BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS
    cutoff = timezone.now() + timedelta(seconds=delay)
    if Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK, run_at__lte=cutoff).exists():
        return

    refresh_blacklist_snapshot(schedule=delay)
    logger.info("Scheduled a blacklist snapshot rebuild in %ds after approving %s",
                delay, instance.name)
