"""Wikipedia results served from our own index, instead of from an HTTP cache on disk.

get_wiki_results() used to cache through mwmbl.utils.request_cache, a requests-cache
*filesystem* session rooted at REQUEST_CACHE_PATH - the same volume as the index - with a
ten-week expiry, no LRU, no size cap and nothing ever calling delete(expired=True). It grew
without bound (the dev copy reached 91,000 files / 4.5 GB), it wrote the raw user query to
disk inside the stored request URL, and it did not remove the call: a miss was still a
blocking HTTP round trip on the search path.

Instead: after a call to the Wikipedia search endpoint, the results are written into the
index under the query's unigrams and bigrams, exactly as Super Search already writes its
own results. A later search that the index answers with enough Wikipedia results skips the
API call altogether.

Privacy - what may be written under a query-derived term:

  * a document only takes a term whose words it *all contains*
    (index_results_against_query's containment rule), so the term -> document link is
    derivable from the document itself and says nothing about the query that produced it;
  * terms are at most WIKI_INDEX_MAX_TERM_TOKENS words, so the query as a whole is never
    stored as one phrase;
  * optionally (WIKI_INDEX_REQUIRE_EXISTING_TERM) only terms the index already holds
    documents for, so nothing is written that the corpus did not already contain.

Note that get_wiki_results() stamps each Document's `term` with the *raw, untokenized
query*. That value must never reach the index. store_wiki_results() goes through
index_results_against_query(), which rebuilds every Document with a derived term, and
test_wiki_index_cache asserts it - the failure would be silent.

The write happens in the request, not in a background task. Two reasons that is safe:

  * Cost. Storing costs about 1.2 ms per query term - 1.3 ms for a one-word query, ~10 ms
    for a five-word one - and it only runs on a miss, where we have just paid 100-500 ms
    for the Wikipedia call itself. The pages it writes are the ones retrieval just read,
    so they are already warm.
  * Correctness. An index page write is a read-modify-write, and an unlocked writer that
    catches another mid-store reads an empty page and stores over everything on it. That is
    now serialised by a POSIX record lock over the page's bytes - see TinyIndex.locked_page.
"""
from logging import getLogger
from pathlib import Path

from django.conf import settings

from mwmbl.indexer.index_batches import index_results_against_query
from mwmbl.tinysearchengine.indexer import Document, DocumentState
from mwmbl.tokenizer import get_bigrams, tokenize

logger = getLogger(__name__)


WIKI_DOMAIN = "en.wikipedia.org"
WIKI_STATES = frozenset({DocumentState.FROM_WIKI, DocumentState.FROM_WIKI_APPROVED})


def is_wiki_document(document: Document) -> bool:
    return bool(document.url) and WIKI_DOMAIN in document.url


def count_wiki_results(documents: list[Document]) -> int:
    return sum(1 for document in documents if is_wiki_document(document))


def count_stored_wiki_results(documents: list[Document]) -> int:
    """Only documents a previous API call put in the index, not organically crawled ones."""
    return sum(1 for document in documents
               if is_wiki_document(document) and document.state in WIKI_STATES)


def query_terms(query: str) -> list[str]:
    """The unigrams and bigrams a query's results are filed under - the gate's denominator."""
    tokens = tokenize(query)
    return tokens + get_bigrams(len(tokens), tokens)


def wiki_documents_by_term(query: str, index_results: list[Document],
                           stored_only: bool = True) -> dict[str, list[Document]]:
    """Each query term mapped to the Wikipedia documents the index returned under it.

    Every term appears, including ones with nothing - a term the index cannot answer is
    exactly the signal a breadth-based gate needs, so it must count as a zero rather than
    being dropped from the average.
    """
    by_term: dict[str, list[Document]] = {term: [] for term in query_terms(query)}
    for document in index_results:
        if document.term not in by_term or not is_wiki_document(document):
            continue
        if stored_only and document.state not in WIKI_STATES:
            continue
        by_term[document.term].append(document)
    return by_term


def per_term_best(by_term: dict[str, list[Document]], score: callable) -> list[float]:
    """The best score among each term's Wikipedia documents; 0.0 for a term with none.

    Scores the whole set in one call rather than per term, because `score` is a model
    prediction and batching it is most of the cost.
    """
    documents, owners = [], []
    for term, term_documents in by_term.items():
        for document in term_documents:
            documents.append(document)
            owners.append(term)

    best: dict[str, float] = {}
    if documents:
        for term, value in zip(owners, score(documents)):
            best[term] = max(best.get(term, 0.0), value)
    return [best.get(term, 0.0) for term in by_term]


def stored_scores(documents: list[Document]) -> list[float]:
    """Document.score as stored.

    Be careful reading a gate built on this. In the index `score` is not a relevance
    number: curated documents carry MAX_CURATED_SCORE - i (~1.1e6), ordinary crawled
    documents carry None, and documents this cache stored carry their rank in Wikipedia's
    own results (3/2/1). Across a mixed set the scales are meaningless. Restricted to
    stored Wikipedia documents it is at least consistent - but then it is nearly a constant,
    so a gate on it collapses into term coverage.
    """
    return [document.score if document.score is not None else 0.0 for document in documents]


# Gates that count documents.
COUNTING_GATES = {
    "raw_candidates": lambda documents, rank, top_n: count_wiki_results(documents),
    "ranked_top_n": lambda documents, rank, top_n: count_wiki_results(rank(documents)[:top_n]),
    "from_wiki_only": lambda documents, rank, top_n: count_stored_wiki_results(rank(documents)[:top_n]),
}

# Gates that reduce a per-term score profile to one number. The threshold is a float.
AGGREGATES = {
    "term_coverage": lambda values: sum(1 for v in values if v > 0) / len(values),
    "mean_max": lambda values: sum(values) / len(values),
    "min_max": min,
    "max_max": max,
}

# gate name -> (aggregate, whether to use the model's relevance score or the stored one)
VALUE_GATES = {
    "term_coverage": ("term_coverage", "stored"),
    "mean_max_ltr": ("mean_max", "ltr"),
    "min_max_ltr": ("min_max", "ltr"),
    "max_max_ltr": ("max_max", "ltr"),
    "mean_max_stored": ("mean_max", "stored"),
}

GATES = list(COUNTING_GATES) + list(VALUE_GATES) + ["never", "always"]


def have_enough_wiki_results(query: str, index_results: list[Document], rank: callable,
                             score: callable = None, gate: str = None,
                             threshold: float = None, top_n: int = None) -> bool:
    """Whether the index already answered this query well enough to skip the API call.

    `rank` orders index candidates the way the ranker would and `score` returns the model's
    relevance for each of a list of documents. Both are passed in rather than taken from a
    ranker, so the gate can be tested and swept without one, and each is only called by the
    gates that need it - they are the only part of this that costs anything.

    Two families of gate:

    *Counting* - how many Wikipedia results came back.
      raw_candidates  cheapest: count wiki URLs among the unranked candidates. A broad
                      query pulls in irrelevant crawled wiki pages, so it over-fires.
      ranked_top_n    rank first, count wiki URLs in the top N. What the user would see.
      from_wiki_only  as ranked_top_n, but counting only documents a previous API call
                      stored - a cache-hit test that ignores crawled coverage.

    *Per-term profile* - how well the index covers each of the query's terms. For every
    unigram and bigram, take the best stored Wikipedia result's score (0 if the term has
    none), then reduce:
      term_coverage   fraction of terms with any stored result at all.
      mean_max_ltr    mean of the per-term bests, by model relevance. Rewards a query whose
                      terms are broadly well covered.
      min_max_ltr     the worst-covered term. Fires only if *every* term is answered.
      max_max_ltr     the best-covered term. The most permissive.
      mean_max_stored as mean_max_ltr but on Document.score - see stored_scores for why
                      that number is close to meaningless.

    And two degenerate ones: `never` always calls (results are still stored, which isolates
    the effect of serving stored documents from that of skipping calls), `always` never
    calls. Which gate preserves NDCG is empirical - see
    mwmbl.rankeval.evaluation.evaluate_wiki_index, which sweeps them.
    """
    gate = settings.WIKI_INDEX_GATE if gate is None else gate
    threshold = settings.WIKI_INDEX_GATE_THRESHOLD if threshold is None else threshold
    top_n = settings.WIKI_INDEX_GATE_TOP_N if top_n is None else top_n

    if gate == "never":
        return False
    if gate == "always":
        return True

    if gate in VALUE_GATES:
        aggregate, source = VALUE_GATES[gate]
        by_term = wiki_documents_by_term(query, index_results)
        if not by_term:
            return False
        score_fn = stored_scores if source == "stored" else score
        return AGGREGATES[aggregate](per_term_best(by_term, score_fn)) >= threshold

    if gate not in COUNTING_GATES:
        raise ValueError(f"Unknown wiki index gate: {gate!r}")
    if threshold <= 0:
        return True
    if not index_results:
        return False
    return COUNTING_GATES[gate](index_results, rank, top_n) >= threshold


def default_index_path() -> str:
    return str(Path(settings.DATA_PATH) / settings.INDEX_NAME)


def store_wiki_results(query: str, documents: list[Document], index_path: str = None,
                       term_exists=None) -> int:
    """File a query's Wikipedia results under the query terms they are allowed to take.

    Returns the number of distinct URLs newly stored - zero is normal and expected:
    Wikipedia spell-corrects and partial-matches, so a returned article need not contain
    the query's words at all, and the containment rule then stores nothing for it.

    `term_exists` overrides how WIKI_INDEX_REQUIRE_EXISTING_TERM decides whether a term is
    already in the corpus. In production the corpus is the index being written to, which
    is the default; an evaluation reading through an overlay has to answer for both halves.

    Never raises. Failing to store costs one re-fetch on the next matching query; it must
    not be able to affect a search response.
    """
    if not documents:
        return 0
    try:
        return index_results_against_query(
            documents, query, index_path or default_index_path(),
            max_term_tokens=settings.WIKI_INDEX_MAX_TERM_TOKENS,
            require_existing_term=settings.WIKI_INDEX_REQUIRE_EXISTING_TERM,
            term_exists=term_exists,
            state=DocumentState.FROM_WIKI,
            keep_score=settings.WIKI_INDEX_KEEP_SCORE,
        )
    except Exception:
        logger.warning("Could not store %d Wikipedia results in the index",
                       len(documents), exc_info=True)
        return 0
