"""Staan web-search results, cached alongside Wikipedia's.

Staan is a general web-search API. It gives us broad recall over pages our own crawl has
never reached, which is the whole reason Combined Search exists.

Deliberately shaped like mwmbl.tinysearchengine.rank.get_wiki_results rather than like the
Super Search adapters: results come back as Documents through
mwmbl.indexer.external_cache, so they are cached, deduplicated and ranked by exactly the
same machinery as every other candidate. The cache file is already namespaced by
DocumentSource, so Staan entries and Wikipedia entries share a page without seeing each
other - see the external_cache module docstring.

Nothing here writes to the search index: these are somebody else's results, and indexing
them is a separate decision with its own quality question.
"""

from logging import getLogger

import requests
from django.conf import settings

from mwmbl.indexer.external_cache import get_cached_external_results, store_external_results
from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.rank import MAX_QUERY_CHARS

logger = getLogger(__name__)


# How many results we keep, and therefore what a cache entry holds. Fixed rather than taken
# from the caller for the same reason NUM_WIKI_RESULTS is (see rank.py): one entry has to
# mean the same thing to every caller, and a pool of a size the model was never fitted to
# is what that constant exists to prevent. The Staan API is not asked for a limit - the
# documented parameters are the query and the market - so this is a cut we make ourselves.
NUM_STAAN_RESULTS = 10

# The score a Staan result carries into the LTR model, top result first, counting down.
#
# `score` becomes the `item_score` feature, so this is a trained-on quantity: the constant
# used to build the training data must be the constant used here. It is the only channel
# through which Staan's own ranking can reach a model that has no source feature. Anchored
# at the top and a function of rank alone, exactly as WIKI_TOP_SCORE is, and on the same
# scale so the two providers' priors are comparable. Changing it means retraining.
STAAN_TOP_SCORE = 6.0


def staan_score(rank: int) -> float:
    """The `score` feature for a Staan result at 0-based `rank`. See STAAN_TOP_SCORE."""
    return STAAN_TOP_SCORE - rank


def _documents(payload: dict, query: str) -> list[Document]:
    # Drop what the external cache would drop (no url or no title) *before* ranking, so the
    # rank a result is scored by here is the rank it is stored at and rescored by on a cache
    # hit. Ranking over the raw list meant a first fetch and every later one disagreed on
    # `score` - a trained-on feature - whenever an unusable result came ahead of a usable one.
    results = [result for result in payload["web"]["results"] if result.get("url") and result.get("title")]
    documents = []
    for rank, result in enumerate(results[:NUM_STAAN_RESULTS]):
        documents.append(
            Document(
                title=result["title"],
                url=result["url"],
                extract=result.get("snippet") or result.get("description", ""),
                score=staan_score(rank),
                term=query,
                source=DocumentSource.STAAN,
            )
        )
    return documents


def get_staan_results(query: str, max_results: int = NUM_STAAN_RESULTS) -> list[Document]:
    """Staan's results for a query, from the external cache where possible.

    Never raises: a provider that is down, slow or unconfigured costs the caller its extra
    recall, never its search.
    """
    query = query[:MAX_QUERY_CHARS]

    # Ahead of the API-key check deliberately: a cache hit costs Staan nothing and needs no
    # credentials, so a deployment that has since dropped the key still serves what it has.
    cached = get_cached_external_results(DocumentSource.STAAN, query)
    if cached is not None:
        # The cache stores the rank Staan gave, not a score - scoring is this function's to
        # do, and doing it here is what makes an entry mean the same thing to every caller.
        staan_results = cached[:max_results]
        for rank, document in enumerate(staan_results):
            document.score = staan_score(rank)
        return staan_results

    if not settings.STAAN_SEARCH_API_KEY:
        logger.warning("STAAN_SEARCH_API_KEY is not configured")
        return []

    try:
        response = requests.get(
            settings.STAAN_SEARCH_URL,
            params={"q": query, "market": settings.STAAN_MARKET},
            headers={"Authorization": f"Bearer {settings.STAAN_SEARCH_API_KEY}"},
            timeout=settings.STAAN_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        documents = _documents(response.json(), query)
    except Exception as e:
        # Never log the exception or the response body: both embed the request URL, and the
        # request URL embeds the user's (private) query. rank.py makes the same trade for
        # Wikipedia - the type is enough to tell a timeout from a 401.
        logger.warning("Failed to fetch Staan results: %s", type(e).__name__)
        return []

    # Only a well-formed answer is stored, including one with no results in it. Every path
    # above returns [] without storing, so a transient failure is never remembered as
    # "Staan has nothing for this".
    store_external_results(DocumentSource.STAAN, query, documents)
    return documents[:max_results]
