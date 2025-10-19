from datetime import timedelta

from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.settings import BLACKLIST_DOMAINS_URL, EXCLUDED_DOMAINS, DOMAIN_BLACKLIST_REGEX
from mwmbl.indexer.blacklist_providers import BlacklistProvider, URLBlacklistProvider, HaGeZiBlacklistProvider, CombinedBlacklistProvider

# Global provider instance that can be configured
_blacklist_provider: BlacklistProvider = None


def get_default_blacklist_provider() -> BlacklistProvider:
    """Get the default blacklist provider configuration."""
    # Use HaGeZi as primary with fallback to old URL for compatibility
    return CombinedBlacklistProvider([
        HaGeZiBlacklistProvider('light'),
        URLBlacklistProvider(BLACKLIST_DOMAINS_URL)
    ])


def set_blacklist_provider(provider: BlacklistProvider) -> None:
    """Set the global blacklist provider (useful for testing)."""
    global _blacklist_provider
    _blacklist_provider = provider


def get_blacklist_provider() -> BlacklistProvider:
    """Get the current blacklist provider."""
    global _blacklist_provider
    if _blacklist_provider is None:
        _blacklist_provider = get_default_blacklist_provider()
    return _blacklist_provider


def get_blacklist_domains() -> set[str]:
    """Get blacklisted domains using the configured provider."""
    provider = get_blacklist_provider()
    return provider.get_blacklisted_domains()


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
