"""
Blacklist providers abstraction for domain blacklisting.

This module provides different ways to check if domains should be blacklisted,
making the system more flexible and testable.
"""

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Set
import requests
from mwmbl.utils import request_cache


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
        # Check excluded domains
        if domain in self.excluded_domains:
            return True
        
        # Check regex patterns (adult/spam content)
        if self.blacklist_regex.search(domain):
            return True
        
        # Trusted domains are never blacklisted
        if domain in self.trusted_domains:
            return False
        
        # Spam detection heuristics for SEO spam domains
        domain_parts = domain.split('.')
        
        # Domains like: brofqpxj.uelinc.com, gzsmjc.fba01.com, 59648.etnomurcia.com
        if (len(domain_parts) == 3 and 
            domain_parts[2] == "com" and 
            len(domain_parts[0]) in {6, 8}):
            return True
        
        # Domains with all numeric first parts
        if len(domain_parts) > 0 and set(domain_parts[0]) <= set("1234567890"):
            return True
        
        return False


class HaGeZiBlacklistProvider(BlacklistProvider):
    """Provider that fetches HaGeZi blocklist."""
    
    # HaGeZi provides several lists, this is their main threat intelligence feeds
    HAGEZI_URLS = {
        'light': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/light.txt',
        'normal': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/normal.txt',
        'pro': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/pro.txt',
        'ultimate': 'https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/ultimate.txt',
    }
    
    def __init__(self, list_type: str = 'light', cache_expire_days: int = 1):
        if list_type not in self.HAGEZI_URLS:
            raise ValueError(f"Invalid list_type. Must be one of: {list(self.HAGEZI_URLS.keys())}")
        
        self.url = self.HAGEZI_URLS[list_type]
        self.cache_expire_days = cache_expire_days
        self._cached_domains = None
    
    def _get_blacklisted_domains(self) -> Set[str]:
        """Fetch HaGeZi blacklist with caching."""
        if self._cached_domains is not None:
            return self._cached_domains
            
        with request_cache(expire_after=timedelta(days=self.cache_expire_days)) as session:
            try:
                response = session.get(self.url)
                response.raise_for_status()
                
                # Parse HaGeZi format - one domain per line, skip comments and empty lines
                domains = set()
                for line in response.text.split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        domains.add(line)
                
                self._cached_domains = domains
                return domains
            except requests.RequestException as e:
                # Log the error but don't fail - return empty set as fallback
                print(f"Warning: Failed to fetch HaGeZi blacklist from {self.url}: {e}")
                self._cached_domains = set()
                return set()
    
    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if domain is in the HaGeZi blacklist."""
        domains = self._get_blacklisted_domains()
        return domain in domains


class CombinedBlacklistProvider(BlacklistProvider):
    """Provider that combines multiple blacklist providers."""
    
    def __init__(self, providers: list[BlacklistProvider]):
        self.providers = providers
    
    def is_domain_blacklisted(self, domain: str) -> bool:
        """Check if domain is blacklisted by any provider."""
        for provider in self.providers:
            try:
                if provider.is_domain_blacklisted(domain):
                    return True
            except Exception as e:
                print(f"Warning: Error from blacklist provider {provider.__class__.__name__}: {e}")
                continue
        
        return False
