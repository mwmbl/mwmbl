import re

from django.db import migrations, models

# Frozen copies of mwmbl.utils.parse_url/normalize_domain/bare_host: a migration has to keep
# producing the same data forever, so it must not depend on application code that is free to
# change. Matches migration 0032, which normalised the domain submission names.
URL_REGEX = re.compile("^(([^:/?#]+):)?(//([^/?#]*)|///)?([^?#]*)(\\?[^#]*)?(#.*)?")
DOMAIN_END_REGEX = re.compile(r"[/?#]")

BATCH_SIZE = 1000


def bare_host(domain_or_url: str) -> str:
    netloc = URL_REGEX.match(domain_or_url).group(4)
    if not netloc:
        netloc = DOMAIN_END_REGEX.split(domain_or_url, maxsplit=1)[0]
    host = netloc.lower()
    return host[4:] if host.startswith("www.") else host


def backfill_domains(apps, schema_editor):
    """Derive the host for every existing vote, in batches.

    Written before the index is created, so the whole table is scanned once rather than
    maintaining an index through the rewrite.
    """
    SearchResultVote = apps.get_model("mwmbl", "SearchResultVote")
    batch = []
    for vote in SearchResultVote.objects.all().only("id", "url").iterator(chunk_size=BATCH_SIZE):
        vote.domain = bare_host(vote.url)
        batch.append(vote)
        if len(batch) >= BATCH_SIZE:
            SearchResultVote.objects.bulk_update(batch, ["domain"])
            batch = []
    if batch:
        SearchResultVote.objects.bulk_update(batch, ["domain"])


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0035_moderationmodelartifact_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='searchresultvote',
            name='domain',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.RunPython(backfill_domains, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='searchresultvote',
            index=models.Index(fields=['domain', 'vote_type'], name='mwmbl_searc_domain_7f4ebd_idx'),
        ),
    ]
