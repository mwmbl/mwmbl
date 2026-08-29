"""
Repair submissions whose status is the moderator-facing *action* rather than a status, and
constrain the column so it cannot happen again.

An external moderation client posted "APPROVE"/"REJECT" - the words it shows the moderator -
where the model's choices are "APPROVED"/"REJECTED". Django checks choices in full_clean(),
which save() never calls, and generates no database constraint, so the words landed in the
column verbatim. A submission holding one matches nothing: not the pending queue, not the
approved set the curated domains (and so the blacklist override) are built from, not the
moderation history, not the training data. The decision simply vanished. 1,785 rows across
1,618 domains were written this way in production before this ran.

The rewrite is not a guess about intent: these were decisions made by a moderator with the
change_domain_submission_status permission, on the domain the client had just shown them.
This applies them.

Newly approved domains are subtracted from the remote blocklists when the snapshot is next
rebuilt (BLACKLIST_SNAPSHOT_REFRESH_SECONDS, six hours), and get_curated_domains caches for
five minutes, so both catch up on their own rather than being poked from a migration.
"""
from django.db import migrations, models

REPAIRS = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}


def repair_statuses(apps, schema_editor):
    DomainSubmission = apps.get_model("mwmbl", "DomainSubmission")
    for written, intended in REPAIRS.items():
        DomainSubmission.objects.filter(status=written).update(status=intended)


def noop_reverse(apps, schema_editor):
    """Irreversible on purpose: an APPROVED row repaired here is indistinguishable from one
    that was always correct, so there is nothing to put back."""


class Migration(migrations.Migration):

    dependencies = [
        ("mwmbl", "0036_search_result_vote_domain"),
    ]

    operations = [
        # Before the constraints, which the rows this repairs would otherwise violate.
        migrations.RunPython(repair_statuses, noop_reverse),
        migrations.AddConstraint(
            model_name="domainsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=["PENDING", "APPROVED", "REJECTED"]),
                name="domain_submission_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="domainsubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    rejection_reason__in=["", "SPAM", "OFFENSIVE", "LANGUAGE", "OTHER"]),
                name="domain_submission_rejection_reason_valid",
            ),
        ),
    ]
