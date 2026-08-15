"""
Tests for blacklist providers and the abstraction system.
"""

import pytest
import requests
from unittest.mock import patch, MagicMock

from mwmbl.indexer import blacklist_providers
from mwmbl.indexer.blacklist_providers import (
    StaticBlacklistProvider,
    HaGeZiBlacklistProvider,
    AdultContentBlacklistProvider,
    CombinedBlacklistProvider
)
from mwmbl.indexer.blacklist import get_default_blacklist_provider


@pytest.fixture(autouse=True)
def clear_shared_domains_cache():
    """_parsed_domains_cache is a module-level dict shared across every
    RemoteListBlacklistProvider instance (see blacklist_providers.py) so that
    index_documents() can call get_default_blacklist_provider() cheaply on every call.
    Clear it around each test so a mocked response in one test can't leak into another."""
    blacklist_providers._parsed_domains_cache.clear()
    yield
    blacklist_providers._parsed_domains_cache.clear()


def test_static_blacklist_provider():
    """Test StaticBlacklistProvider with is_domain_blacklisted method."""
    test_domains = {'spam.com', 'malware.example'}
    provider = StaticBlacklistProvider(test_domains)
    
    # Test domains that should be blacklisted
    assert provider.is_domain_blacklisted('spam.com') == True
    assert provider.is_domain_blacklisted('malware.example') == True
    
    # Test domain that should not be blacklisted
    assert provider.is_domain_blacklisted('github.com') == False


def test_hagezi_blacklist_provider_success():
    """Test HaGeZiBlacklistProvider with successful HTTP response."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = '''# HaGeZi DNS Blocklist
# Comments should be ignored
spam.com
malware.example

# Another comment
badsite.net
'''
        mock_get.return_value = mock_response

        provider = HaGeZiBlacklistProvider('light')

        # Test domains that should be blacklisted
        assert provider.is_domain_blacklisted('spam.com') == True
        assert provider.is_domain_blacklisted('malware.example') == True
        assert provider.is_domain_blacklisted('badsite.net') == True
        
        # Test domain that should not be blacklisted
        assert provider.is_domain_blacklisted('github.com') == False


def test_adult_content_blacklist_provider_success():
    """Test AdultContentBlacklistProvider parses hosts-format lines correctly."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = '''# Title: Porn Block List
# Homepage: https://github.com/blocklistproject/Lists

0.0.0.0 fineartteens.com
0.0.0.0 milforia.com
'''
        mock_get.return_value = mock_response

        provider = AdultContentBlacklistProvider()

        assert provider.is_domain_blacklisted('fineartteens.com') == True
        assert provider.is_domain_blacklisted('milforia.com') == True
        assert provider.is_domain_blacklisted('github.com') == False


def test_hagezi_blacklist_provider_invalid_type():
    """Test HaGeZiBlacklistProvider rejects invalid list types."""
    with pytest.raises(ValueError):
        HaGeZiBlacklistProvider('invalid_type')


def test_combined_blacklist_provider():
    """Test CombinedBlacklistProvider with is_domain_blacklisted method."""
    provider1 = StaticBlacklistProvider({'spam.com', 'malware.example'})
    provider2 = StaticBlacklistProvider({'badsite.net', 'phishing.site'})
    
    combined = CombinedBlacklistProvider([provider1, provider2])
    
    # Test domains from both providers
    assert combined.is_domain_blacklisted('spam.com') == True
    assert combined.is_domain_blacklisted('badsite.net') == True
    assert combined.is_domain_blacklisted('phishing.site') == True
    
    # Test domain that should not be blacklisted
    assert combined.is_domain_blacklisted('github.com') == False


def test_combined_blacklist_provider_handles_failures():
    """Test CombinedBlacklistProvider continues even if one provider fails."""
    good_provider = StaticBlacklistProvider({'spam.com'})
    bad_provider = MagicMock()
    bad_provider.is_domain_blacklisted.side_effect = Exception("Provider failure")
    
    combined = CombinedBlacklistProvider([good_provider, bad_provider])
    
    # Should still work with the good provider despite the bad one failing
    assert combined.is_domain_blacklisted('spam.com') == True
    assert combined.is_domain_blacklisted('github.com') == False


def test_integration_with_blacklist_module():
    """Test integration with the main blacklist module."""
    # Test the default provider factory
    default_provider = get_default_blacklist_provider()
    assert default_provider is not None
    
    # Test that it can check domains
    # Test with a domain that should be blacklisted by built-in rules
    assert default_provider.is_domain_blacklisted('59648.etnomurcia.com') == True
    
    # Test with a domain that should not be blacklisted
    assert default_provider.is_domain_blacklisted('github.com') == False


def test_remote_lists_do_not_use_the_shared_filesystem_request_cache():
    """These lists are tens of megabytes. mwmbl.utils.request_cache is a *filesystem* cache
    rooted at settings.REQUEST_CACHE_PATH, on the same volume as the index - caching them
    there fills that volume, and once it is full every other user of request_cache breaks,
    including get_wiki_results() on the live search path (which then retries a live fetch
    4x per uncached query until the worker dies). Regression test: fetch must not go
    through request_cache."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = "spam.com\n"
        mock_get.return_value = mock_response

        # If the module still imported request_cache, patching it would succeed; assert the
        # name is gone from the module so this can't silently regress.
        import mwmbl.indexer.blacklist_providers as bp
        assert not hasattr(bp, "request_cache"), \
            "blacklist_providers must not use the shared filesystem request_cache"

        provider = HaGeZiBlacklistProvider('light')
        assert provider.is_domain_blacklisted('spam.com') is True
        assert mock_get.called


def test_fetch_failure_falls_back_to_last_good_list():
    """A transient fetch failure must not silently blank out the provider's coverage."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = "spam.com\n"
        mock_get.return_value = mock_response
        provider = HaGeZiBlacklistProvider('light')
        assert provider.is_domain_blacklisted('spam.com') is True

    # Expire the module-level cache so the next call refetches, and make that fetch fail.
    blacklist_providers._parsed_domains_cache[provider.url] = (
        -10**9, blacklist_providers._parsed_domains_cache[provider.url][1])
    with patch('mwmbl.indexer.blacklist_providers.requests.get',
               side_effect=requests.RequestException("boom")):
        assert provider.is_domain_blacklisted('spam.com') is True  # still covered
