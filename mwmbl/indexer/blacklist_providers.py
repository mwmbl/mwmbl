"""
Blacklist providers abstraction for domain blacklisting.

This module provides different ways to check if domains should be blacklisted,
making the system more flexible and testable.
"""

from abc import ABC, abstractmethod
from typing import Callable, Optional, Set
import requests


def domain_and_parents(domain: str) -> list[str]:
    """The domain itself followed by each of its parent domains, most specific first.

    The remote lists are apex rules, not host rules - HaGeZi's file header says
    "Syntax: Domains (without subdomains)" and it lives under wildcard/ - so an entry for
    example.com is meant to cover www.example.com and every other subdomain. get_domain()
    hands us the full host, so testing only that host would let every subdomain of a
    blacklisted domain through, starting with the www. form that most sites actually
    serve. Blacklisting is therefore membership of *any* of these.

    The last candidate has two labels: a single-label candidate is a bare TLD, and
    blacklisting one would take out every domain under it.

        >>> domain_and_parents("nyou.booru.org")
        ['nyou.booru.org', 'booru.org']
    """
    parts = domain.split('.')
    if len(parts) < 2:
        return [domain]
    return ['.'.join(parts[i:]) for i in range(len(parts) - 1)]


def domains_to_unblock(curated_domains: Set[str]) -> Set[str]:
    """The blocklist entries these curated domains should clear.

    DomainSubmissionForm.clean_name stores whatever netloc was submitted, so an approval can
    arrive as www.contactmusic.com while the blocklist holds the apex contactmusic.com.
    Stripping the www. covers that; nothing deeper is stripped, because clearing an apex
    entry on the strength of an approved subdomain would unblock every one of its siblings.
    """
    unblocked = set(curated_domains)
    unblocked.update(domain[len("www."):] for domain in curated_domains if domain.startswith("www."))
    return unblocked


class BlacklistProvider(ABC):
    """Abstract base class for blacklist providers."""
    
    @abstractmethod
    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if a domain should be blacklisted."""
        pass


class StaticBlacklistProvider(BlacklistProvider):
    """Provider that uses a static set of domains."""
    
    def __init__(self, domains: Set[str]):
        self.domains = domains.copy()
    
    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if domain is in the static blacklist."""
        return domain in self.domains


class BuiltInRulesBlacklistProvider(BlacklistProvider):
    """Provider that implements the built-in spam detection and exclusion rules."""
    
    def __init__(self):
        # Import here to avoid circular imports
        from mwmbl.settings import EXCLUDED_DOMAINS, DOMAIN_BLACKLIST_REGEX
        from mwmbl.hn_top_domains_filtered import DOMAINS
        
        self.excluded_domains = EXCLUDED_DOMAINS
        self.blacklist_regex = DOMAIN_BLACKLIST_REGEX
        self.trusted_domains = DOMAINS
    
    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if domain should be blacklisted based on built-in rules."""
        # Check excluded domains. Matched as a suffix, so an apex entry covers its
        # subdomains - the entries written with an explicit www. prefix still only match
        # that host and below, which is what they were added for.
        if any(candidate in self.excluded_domains for candidate in domain_and_parents(domain)):
            return True
        
        # Check regex patterns (adult/spam content)
        if self.blacklist_regex.search(domain):
            return True
        
        # Trusted domains are never blacklisted
        if domain in self.trusted_domains:
            return False

        # Spam detection heuristics for SEO spam domains
        #
        # There used to be a second rule here matching any .com host whose first label was
        # 6 or 8 characters long (aimed at brofqpxj.uelinc.com, gzsmjc.fba01.com). It was
        # dropped deliberately: label length says nothing about spam, and it took out
        # groups.google.com, images.example.com and every other three-label .com site with
        # a 6- or 8-character subdomain. Generated-hostname spam is better handled by the
        # threat feed than by guessing at label lengths.
        domain_parts = domain.split('.')

        # Domains with all-numeric generated-looking subdomains, e.g. 59648.etnomurcia.com.
        # Restricted to len > 2 so this doesn't catch a numeric *apex* domain like 350.org,
        # where the numeric part is the actual site name rather than a generated subdomain.
        if len(domain_parts) > 2 and set(domain_parts[0]) <= set("1234567890"):
            return True
        
        return False


class RemoteListBlacklistProvider(BlacklistProvider):
    """Provider that fetches a remote newline-delimited domain list.

    Handles both plain domain-per-line lists and hosts-file format
    (``0.0.0.0 domain.com``), which is what most public blocklists use.

    Downloading and parsing one of these lists costs tens of megabytes of transfer and
    ~100 MB of resident domain strings, so an instance is deliberately expensive to
    construct-and-use and the caller owns how long that cost is amortised over. There is
    no cache behind this module: an earlier version kept the parsed set in a module-level
    dict keyed by URL, which made `get_default_blacklist_provider()` look cheap enough to
    call from anywhere - including a request handler, where the first call blocks on the
    download and the worker then holds the domain strings for its lifetime. Instead, the
    fetch happens once per instance, and the only long-lived instance lives in the
    background snapshot task. Everything on the search and indexing paths reads the
    published snapshot (mwmbl.indexer.blacklist_snapshot) instead of coming here.
    """

    def __init__(self, url: str):
        self.url = url
        self._domains: Optional[Set[str]] = None

    def _get_blacklisted_domains(self) -> Set[str]:
        """The parsed domain set, fetched on first use and held for this instance's life.

        Raises requests.RequestException if the list cannot be fetched. Callers must not
        turn that into an empty set: a silently empty list is indistinguishable from a
        list that blacklists nothing, and build_snapshot() would publish the result as
        though it were complete.
        """
        if self._domains is not None:
            return self._domains

        # Deliberately NOT using mwmbl.utils.request_cache here. That is a *filesystem*
        # cache rooted at settings.REQUEST_CACHE_PATH = f"{DATA_PATH}/request_cache" - the
        # same volume that holds the multi-hundred-GB index. These blocklists are tens of
        # megabytes each, so caching them there fills that volume, and once it is full
        # *every* user of request_cache breaks - and it shares that volume with the index
        # itself. (get_wiki_results() used to be the worst of those, retrying a live fetch
        # 4 times per uncached query on the search path; it now has a cache of its own, see
        # mwmbl.indexer.wiki_cache.) A big periodic download has no business in a shared
        # small-response cache on the index volume.
        response = requests.get(self.url, timeout=60)
        response.raise_for_status()

        domains = set()
        for line in response.text.split('\n'):
            line = line.split('#')[0].strip()
            if not line:
                continue
            # Hosts format: "0.0.0.0 domain.com" or "127.0.0.1 domain.com"
            parts = line.split()
            domains.add(parts[1] if len(parts) >= 2 and parts[0] in ('0.0.0.0', '127.0.0.1') else parts[0])

        self._domains = domains
        return domains

    def is_domain_blacklisted(self, domain: str) -> bool:
        domains = self._get_blacklisted_domains()
        return any(candidate in domains for candidate in domain_and_parents(domain))


class HaGeZiBlacklistProvider(RemoteListBlacklistProvider):
    """Provider that fetches a HaGeZi blocklist.

    NB: upstream restructured their repo from domains/{tier}.txt to
    wildcard/{tier}-onlydomains.txt at some point - the old domains/*.txt URLs now
    404 silently (request_cache treats that as "list temporarily empty", not an
    error), so whichever tier was previously configured here had been contributing
    nothing.

    The 'light'/'normal'/'pro'/'ultimate' tiers are HaGeZi's general "Multi" lists:
    ads, affiliate, tracking, telemetry, and only *also* phishing/malware/scam mixed
    in. They're built for personal ad-blocking, not "should this be excluded from a
    search index" - they flag plenty of legitimate businesses purely for running
    analytics/trackers (e.g. baremetrics.com, covidtracking.com), which is a false
    positive for our purposes even though it's correct behaviour for a DNS ad-blocker.

    The 'tif'/'tif_medium'/'tif_mini' tiers are HaGeZi's dedicated Threat
    Intelligence Feed - malware, phishing, scam and C2 domains specifically, no
    general ad/tracker noise. That's the actually-relevant category here, and
    tif_medium (390k entries) tested clean against every legitimate domain in our
    Search Console sample. get_default_blacklist_provider() uses 'tif_medium'.
    """

    HAGEZI_URLS = {
        'light': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/light-onlydomains.txt',
        'normal': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/multi-onlydomains.txt',
        'pro': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/pro-onlydomains.txt',
        'ultimate': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/ultimate-onlydomains.txt',
        'tif': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif-onlydomains.txt',
        'tif_medium': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif.medium-onlydomains.txt',
        'tif_mini': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif.mini-onlydomains.txt',
    }

    def __init__(self, list_type: str = 'tif_medium'):
        if list_type not in self.HAGEZI_URLS:
            raise ValueError(f"Invalid list_type. Must be one of: {list(self.HAGEZI_URLS.keys())}")

        super().__init__(self.HAGEZI_URLS[list_type])


class AdultContentBlacklistProvider(RemoteListBlacklistProvider):
    """Provider that fetches The Block List Project's maintained adult-content domain list.

    See https://github.com/blocklistproject/Lists - hosts-format, ~950k domains,
    updated regularly. Covers the general adult-content gap that the built-in
    regex/domain rules (aimed at spam and a handful of known-bad domains) don't.
    """

    URL = 'https://raw.githubusercontent.com/blocklistproject/Lists/master/porn.txt'

    def __init__(self):
        super().__init__(self.URL)


class CombinedBlacklistProvider(BlacklistProvider):
    """Provider that combines multiple blacklist providers.

    `get_exempt_domains` supplies the domains a moderator has approved, which override every
    sub-provider. It is a callable rather than a set because the caller decides where the
    approved names come from - the database in the server, an HTTP fetch in the standalone
    crawler - and the result is resolved once and held, like the remote lists themselves.
    """

    def __init__(self, providers: list[BlacklistProvider],
                 get_exempt_domains: Optional[Callable[[], Set[str]]] = None):
        self.providers = providers
        self.get_exempt_domains = get_exempt_domains
        self._exempt_domains: Optional[Set[str]] = None

    def _is_exempt(self, domain: str) -> bool:
        if self.get_exempt_domains is None:
            return False
        if self._exempt_domains is None:
            self._exempt_domains = domains_to_unblock(self.get_exempt_domains())
        apex = domain[len("www."):] if domain.startswith("www.") else domain
        return domain in self._exempt_domains or apex in self._exempt_domains

    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if domain is blacklisted by any provider."""
        if self._is_exempt(domain):
            return False

        for provider in self.providers:
            try:
                if provider.is_domain_blacklisted(domain):
                    return True
            except Exception as e:
                print(f"Warning: Error from blacklist provider {provider.__class__.__name__}: {e}")
                continue
        
        return False
