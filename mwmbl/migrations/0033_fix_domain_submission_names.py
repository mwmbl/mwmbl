import re

from django.db import migrations

# Frozen copies of mwmbl.utils.parse_url/normalize_domain: a migration has to keep producing the
# same data forever, so it must not depend on application code that is free to change.
URL_REGEX = re.compile("^(([^:/?#]+):)?(//([^/?#]*)|///)?([^?#]*)(\\?[^#]*)?(#.*)?")
DOMAIN_END_REGEX = re.compile(r"[/?#]")
VALID_DOMAIN_REGEX = re.compile(r"^[\w-]{1,63}(\.[\w-]{1,63})+$")

BATCH_SIZE = 1000


def normalize_domain(domain_or_url: str) -> str:
    netloc = URL_REGEX.match(domain_or_url).group(4)
    if not netloc:
        netloc = DOMAIN_END_REGEX.split(domain_or_url, maxsplit=1)[0]
    return netloc.lower()


def fix_names(apps, schema_editor):
    """
    Re-run the normalisation from migration 0032, which has already been applied in production with
    a version that neither lowercased names nor dealt with names that cannot be reduced to a domain
    at all (e.g. "http://"). Submissions still holding such a name are deleted: there is no domain
    to recover from them, and the submission list reverses the domain URL for every submission, so
    a single one of them makes the whole page fail.
    """
    DomainSubmission = apps.get_model("mwmbl", "DomainSubmission")
    submissions_to_update = []
    ids_to_delete = []
    for submission in DomainSubmission.objects.all().iterator(chunk_size=BATCH_SIZE):
        normalized_name = normalize_domain(submission.name)
        if VALID_DOMAIN_REGEX.fullmatch(normalized_name) is None:
            ids_to_delete.append(submission.id)
        elif normalized_name != submission.name:
            submission.name = normalized_name
            submissions_to_update.append(submission)

    DomainSubmission.objects.bulk_update(submissions_to_update, ["name"], batch_size=BATCH_SIZE)
    for start in range(0, len(ids_to_delete), BATCH_SIZE):
        DomainSubmission.objects.filter(id__in=ids_to_delete[start:start + BATCH_SIZE]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("mwmbl", "0032_normalize_domain_submission_names"),
    ]

    operations = [
        migrations.RunPython(fix_names, migrations.RunPython.noop),
    ]
