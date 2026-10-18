"""Recording the crawler devices a user submits results from."""

from mwmbl.models import Device

# Devices are cosmetic: they exist so a user can tell their own crawlers apart in the UI.
# Nothing depends on the list being complete, so a bound is cheap insurance against a
# client that varies the name it reports - whether by accident (a container with a random
# hostname per start) or deliberately - filling the table one row per submission.
MAX_DEVICES_PER_USER = 50

HOSTNAME_MAX_LENGTH = Device._meta.get_field("hostname").max_length


def record_device(user, hostname: str) -> None:
    """Note that `user` is crawling from `hostname`, creating the device if it is new.

    The name is truncated rather than rejected. It arrives on the same request as the
    crawl results, Django does not validate field lengths on save(), and an over-long name
    would otherwise raise DataError before indexing and lose every page in the submission -
    a heavy price for a label.

    Over the cap, the least recently seen devices are dropped rather than the new one being
    refused, so the list always reflects what the user is actually crawling with now.
    """
    _, created = Device.objects.update_or_create(user=user, hostname=hostname[:HOSTNAME_MAX_LENGTH], defaults={})
    if not created:
        return

    stale_ids = list(
        Device.objects.filter(user=user).order_by("-last_seen").values_list("id", flat=True)[MAX_DEVICES_PER_USER:]
    )
    if stale_ids:
        Device.objects.filter(id__in=stale_ids).delete()
