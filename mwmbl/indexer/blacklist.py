from datetime import timedelta

from requests_cache import CachedSession

from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.settings import BLACKLIST_DOMAINS_URL, EXCLUDED_DOMAINS, DOMAIN_BLACKLIST_REGEX


def get_blacklist_domains():
    with CachedSession(expire_after=timedelta(days=1)) as session:
        response = session.get(BLACKLIST_DOMAINS_URL)
        return set(response.text.split())


def is_domain_blacklisted(domain: str, blacklist_domains: set[str]):
    if domain in EXCLUDED_DOMAINS or DOMAIN_BLACKLIST_REGEX.search(domain) is not None \
            or domain in blacklist_domains:
        return True

    if domain in DOMAINS:
        return False

    # TODO: this is to filter out spammy domains that look like:
    #           brofqpxj.uelinc.com
    #           gzsmjc.fba01.com
    #           59648.etnomurcia.com
    #
    #       Eventually we can figure out a better way to identify SEO spam
    domain_parts = domain.split('.')
    if (len(domain_parts) == 3 and domain_parts[2] == "com" and len(domain_parts[0]) in {6, 8}) or (
        set(domain_parts[0]) <= set("1234567890")
    ):
        return True
