from django.db import migrations

from mwmbl.utils import normalize_domain


def normalize_names(apps, schema_editor):
    DomainSubmission = apps.get_model("mwmbl", "DomainSubmission")
    submissions_to_update = []
    for submission in DomainSubmission.objects.all():
        normalized_name = normalize_domain(submission.name)
        if normalized_name != submission.name and normalized_name != "":
            submission.name = normalized_name
            submissions_to_update.append(submission)
    DomainSubmission.objects.bulk_update(submissions_to_update, ["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("mwmbl", "0031_dedupe_background_task_schedules"),
    ]

    operations = [
        migrations.RunPython(normalize_names, migrations.RunPython.noop),
    ]
