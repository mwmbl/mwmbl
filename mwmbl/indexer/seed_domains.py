"""Seed domains: the domains Staan returns for seed crawls, scored for crawlers.

Two things make a domain worth crawling. It should still have pages the index lacks: its
new-page score is the share of a crawl's per-domain budget (SEED_CRAWL_MAX_PAGES_PER_DOMAIN)
that turned out to be new pages, averaged over its last SEED_DOMAIN_RECENT_CRAWLS crawls, so
it falls as crawls saturate the domain. And it should be likely to answer searches: Staan
returning it often, across everyone's seed queries, is the evidence for that. A domain's
score is the product of the two, and GET /api/v2/combined-search/seed-domains lists the
domains by it.

The user whose crawl first met a domain is kept against it, so that discovering a domain
that turns out well can be rewarded later. Nothing shows it yet.
"""

from django.conf import settings
from django.db import transaction
from django.db.models import F

from mwmbl.models import SeedDomain, SeedDomainCrawl


def new_page_score(pages_indexed: int) -> float:
    """The share of a crawl's per-domain budget that pages_indexed new pages make."""
    return pages_indexed / settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN


def register_seed_domains(user_id: int, staan_results: dict[str, int], count_staan_results: bool) -> set[str]:
    """Register a seed crawl's domains, returning those it discovered.

    staan_results maps each domain to how many of the query's Staan results it had; they are
    added to the domains' totals when count_staan_results is set.
    """
    known = set(SeedDomain.objects.filter(domain__in=staan_results).values_list("domain", flat=True))
    discovered = set(staan_results) - known
    with transaction.atomic():
        # ignore_conflicts because a domain may have been discovered since the read above.
        SeedDomain.objects.bulk_create(
            [SeedDomain(domain=domain, discovered_by_id=user_id) for domain in sorted(discovered)],
            ignore_conflicts=True,
        )
        if count_staan_results:
            for domain, count in staan_results.items():
                SeedDomain.objects.filter(domain=domain).update(
                    staan_results=F("staan_results") + count,
                    score=F("new_page_score") * (F("staan_results") + count),
                )
    return discovered


def record_seed_domain_crawls(pages_indexed: dict[str, int]) -> None:
    """Record a finished crawl's new pages per domain and rescore those domains.

    pages_indexed holds every domain the crawl covered, including those that gave nothing:
    a crawl that finds a domain exhausted is what lowers its score.
    """
    with transaction.atomic():
        seed_domains = SeedDomain.objects.select_for_update().filter(domain__in=pages_indexed)
        for seed_domain in seed_domains:
            SeedDomainCrawl.objects.create(seed_domain=seed_domain, pages_indexed=pages_indexed[seed_domain.domain])
            recent_pages = list(
                seed_domain.crawls.order_by("-crawled_at", "-id").values_list("pages_indexed", flat=True)[
                    : settings.SEED_DOMAIN_RECENT_CRAWLS
                ]
            )
            seed_domain.new_page_score = new_page_score(sum(recent_pages)) / len(recent_pages)
            seed_domain.score = seed_domain.new_page_score * seed_domain.staan_results
            seed_domain.save(update_fields=["new_page_score", "score"])


def top_seed_domains(limit: int) -> list[SeedDomain]:
    return list(SeedDomain.objects.order_by("-score", "domain")[:limit])
