"""
Blacklist utilities for domain filtering.

This module provides utility functions for creating default blacklist providers.
The main logic has been moved to blacklist_providers.py for better modularity.
"""

from mwmbl.indexer.blacklist_providers import (
    BuiltInRulesBlacklistProvider, 
    HaGeZiBlacklistProvider, 
    CombinedBlacklistProvider,
    BlacklistProvider
)


def get_default_blacklist_provider() -> BlacklistProvider:
    """Get the default blacklist provider configuration."""
    return CombinedBlacklistProvider([
        BuiltInRulesBlacklistProvider(),
        HaGeZiBlacklistProvider('light')
    ])
