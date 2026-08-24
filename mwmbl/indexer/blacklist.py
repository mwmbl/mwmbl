"""
Blacklist utilities for domain filtering.

This module provides utility functions for creating default blacklist providers.
The main logic has been moved to blacklist_providers.py for better modularity.
"""

from typing import Callable, Optional, Set

from mwmbl.curated_domains import get_curated_domains
from mwmbl.indexer.blacklist_providers import (
    BuiltInRulesBlacklistProvider,
    HaGeZiBlacklistProvider,
    AdultContentBlacklistProvider,
    CombinedBlacklistProvider,
    BlacklistProvider
)


def get_default_blacklist_provider(
        get_exempt_domains: Optional[Callable[[], Set[str]]] = None) -> BlacklistProvider:
    """Get the default blacklist provider configuration.

    Domains a moderator has approved override the lists. Callers without a database - the
    standalone crawler - pass their own source of approved names; the default reads them
    from DomainSubmission and returns nothing when there is no database.
    """
    return CombinedBlacklistProvider(
        [
            BuiltInRulesBlacklistProvider(),
            HaGeZiBlacklistProvider('tif_medium'),
            AdultContentBlacklistProvider(),
        ],
        get_exempt_domains=get_exempt_domains if get_exempt_domains is not None else get_curated_domains,
    )
