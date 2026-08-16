"""
Tests for blacklist providers and the abstraction system.
"""

import re

import pytest
import requests
from unittest.mock import patch, MagicMock

from mwmbl.indexer.blacklist_providers import (
    BuiltInRulesBlacklistProvider,
    StaticBlacklistProvider,
    HaGeZiBlacklistProvider,
    AdultContentBlacklistProvider,
    CombinedBlacklistProvider,
    domain_and_parents,
)
from mwmbl.indexer.blacklist import get_default_blacklist_provider


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


def test_fetch_failure_propagates():
    """A failed fetch must not look like a list that blacklists nothing.

    build_snapshot() publishes the union of these lists to every search worker, so a
    provider that swallowed the error and returned an empty set would replace a good
    snapshot with one missing its entire contribution."""
    provider = HaGeZiBlacklistProvider('light')
    with patch('mwmbl.indexer.blacklist_providers.requests.get',
               side_effect=requests.RequestException("boom")):
        with pytest.raises(requests.RequestException):
            provider.is_domain_blacklisted('spam.com')


def test_the_list_is_fetched_once_per_instance():
    """The cost of a remote list is owned by whoever holds the provider.

    There is no cache behind the module - that made get_default_blacklist_provider() look
    cheap enough to call from a request handler. Reuse comes from holding the instance."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = "spam.com\n"
        mock_get.return_value = mock_response

        provider = HaGeZiBlacklistProvider('light')
        assert provider.is_domain_blacklisted('spam.com') is True
        assert provider.is_domain_blacklisted('other.com') is False
        assert mock_get.call_count == 1

        # A second instance is a second download - nothing is shared between them.
        assert HaGeZiBlacklistProvider('light').is_domain_blacklisted('spam.com') is True
        assert mock_get.call_count == 2


def test_hosts_lines_with_trailing_comments_keep_the_domain():
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = "0.0.0.0 spam.com # added 2026-01-01\n# a comment line\nplain.com\n"
        mock_get.return_value = mock_response

        provider = AdultContentBlacklistProvider()
        assert provider._get_blacklisted_domains() == {'spam.com', 'plain.com'}


def test_domain_and_parents():
    assert domain_and_parents("nyou.booru.org") == ["nyou.booru.org", "booru.org"]
    assert domain_and_parents("booru.org") == ["booru.org"]
    # Never down to a bare TLD, which would take out everything under it.
    assert "org" not in domain_and_parents("a.b.c.org")
    assert domain_and_parents("localhost") == ["localhost"]


def test_a_remote_list_entry_covers_subdomains():
    """The lists are apex rules, so www.spam.com must be caught by an entry for spam.com."""
    with patch('mwmbl.indexer.blacklist_providers.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.text = "spam.com\n"
        mock_get.return_value = mock_response

        provider = HaGeZiBlacklistProvider('light')
        assert provider.is_domain_blacklisted('spam.com') is True
        assert provider.is_domain_blacklisted('www.spam.com') is True
        assert provider.is_domain_blacklisted('cdn.images.spam.com') is True
        assert provider.is_domain_blacklisted('notspam.com') is False
        assert provider.is_domain_blacklisted('spam.com.example.org') is False


def test_excluded_domains_cover_subdomains():
    """EXCLUDED_DOMAINS entries are written apex-first; the www. form must still match."""
    with patch.object(BuiltInRulesBlacklistProvider, '__init__', lambda self: None):
        provider = BuiltInRulesBlacklistProvider()
        provider.excluded_domains = {'fineartteens.com'}
        provider.blacklist_regex = re.compile(r"^$")
        provider.trusted_domains = set()

        assert provider.is_domain_blacklisted('fineartteens.com') is True
        assert provider.is_domain_blacklisted('www.fineartteens.com') is True
        assert provider.is_domain_blacklisted('otherfineartteens.com') is False
