"""
Tests for blacklist providers and the abstraction system.
"""

import pytest
from unittest.mock import patch, MagicMock

from mwmbl.indexer.blacklist_providers import (
    StaticBlacklistProvider, 
    HaGeZiBlacklistProvider, 
    CombinedBlacklistProvider
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
    with patch('mwmbl.indexer.blacklist_providers.request_cache') as mock_cache:
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '''# HaGeZi DNS Blocklist
# Comments should be ignored
spam.com
malware.example

# Another comment
badsite.net
'''
        mock_session.get.return_value = mock_response
        mock_cache.return_value.__enter__.return_value = mock_session
        
        provider = HaGeZiBlacklistProvider('light')
        
        # Test domains that should be blacklisted
        assert provider.is_domain_blacklisted('spam.com') == True
        assert provider.is_domain_blacklisted('malware.example') == True
        assert provider.is_domain_blacklisted('badsite.net') == True
        
        # Test domain that should not be blacklisted
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
