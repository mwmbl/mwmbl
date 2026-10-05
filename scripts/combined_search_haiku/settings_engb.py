from mwmbl.settings_dev import *  # noqa: F401,F403

# A cache of its own: the external cache is keyed by query alone, so the en-us Staan entries
# in the shared file would otherwise be served back regardless of market.
EXTERNAL_CACHE_INDEX_NAME = "external-cache-engb.tinysearch"
STAAN_MARKET = "en-gb"
