"""The set of domains a human moderator has approved.

Approved DomainSubmissions are the override for the remote blocklists. Those lists are
maintained for DNS ad-blocking and carry false positives that are wrong for a search index
- pudding.cool, contactmusic.com and character.ai are all in The Block List Project's
porn.txt - so an approval here removes the domain from the blacklist rather than merely
outranking it.

This lives outside search_setup because search_setup imports blacklist_snapshot, and the
blacklist code needs to import this. The curated set is only ever read where a blacklist is
*constructed* (build_snapshot, CombinedBlacklistProvider), never per query.
"""
from django.conf import settings
from django.core.cache import cache

from mwmbl.models import DomainSubmission

CURATED_DOMAINS_CACHE_KEY = "curated-domains"
CURATED_DOMAINS_CACHE_TIMEOUT = 300


def get_curated_domains() -> set[str]:
    # The standalone crawler runs django.setup() against settings_crawler, which has no
    # database; it fetches the same names over HTTP instead, see crawl.py.
    if not settings.HAS_DATABASE:
        return set()

    curated_domains = cache.get(CURATED_DOMAINS_CACHE_KEY)
    if curated_domains is None:
        curated_domains = set(DomainSubmission.objects.filter(status="APPROVED").values_list('name', flat=True))
        cache.set(CURATED_DOMAINS_CACHE_KEY, curated_domains, CURATED_DOMAINS_CACHE_TIMEOUT)
    return curated_domains
