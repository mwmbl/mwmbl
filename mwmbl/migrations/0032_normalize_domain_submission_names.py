import re

from django.db import migrations

# Frozen copies of mwmbl.utils.parse_url/normalize_domain: a migration has to keep producing the
# same data forever, so it must not depend on application code that is free to change.
URL_REGEX = re.compile("^(([^:/?#]+):)?(//([^/?#]*)|///)?([^?#]*)(\\?[^#]*)?(#.*)?")
DOMAIN_END_REGEX = re.compile(r"[/?#]")

BATCH_SIZE = 1000


def normalize_domain(domain_or_url: str) -> str:
    netloc = URL_REGEX.match(domain_or_url).group(4)
    if not netloc:
        netloc = DOMAIN_END_REGEX.split(domain_or_url, maxsplit=1)[0]
    return netloc.lower()


def normalize_names(apps, schema_editor):
    DomainSubmission = apps.get_model("mwmbl", "DomainSubmission")
    submissions_to_update = []
    for submission in DomainSubmission.objects.all().iterator(chunk_size=BATCH_SIZE):
        normalized_name = normalize_domain(submission.name)
        if normalized_name != submission.name:
            submission.name = normalized_name
            submissions_to_update.append(submission)
    DomainSubmission.objects.bulk_update(submissions_to_update, ["name"], batch_size=BATCH_SIZE)


class Migration(migrations.Migration):

    dependencies = [
        ("mwmbl", "0031_dedupe_background_task_schedules"),
    ]

    operations = [
        migrations.RunPython(normalize_names, migrations.RunPython.noop),
    ]
