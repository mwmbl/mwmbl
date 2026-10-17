from datetime import timedelta

from django.utils import timezone

from mwmbl.indexer.agreement import Agreement
from mwmbl.models import CrawlerAgreement, MwmblUser

# Long enough to see a crawler's agreement settle over many partners, short enough that the
# table, a row per URL two crawlers share, stays small.
AGREEMENT_RETENTION = timedelta(days=90)


def record_agreements(agreements: set[Agreement], user: MwmblUser, crawler_version: str | None):
    CrawlerAgreement.objects.bulk_create(
        CrawlerAgreement(
            url=agreement.url,
            user=user,
            existing_user_ids=list(agreement.existing_user_ids),
            title_similarity=agreement.title_similarity,
            extract_similarity=agreement.extract_similarity,
            last_crawled_delta=agreement.last_crawled_delta,
            crawler_version=crawler_version,
        )
        for agreement in agreements
    )


def prune_agreements() -> int:
    cutoff = timezone.now() - AGREEMENT_RETENTION
    deleted, _ = CrawlerAgreement.objects.filter(created_at__lt=cutoff).delete()
    return deleted
