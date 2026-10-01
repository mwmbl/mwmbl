"""Combined Search's final ordering: Jev composite + Staan rank.

Jev (TypeSafe's structured-judgment model) answers two Score questions about every
candidate in one request per query: how relevant it is, on the Haiku judge's UK-relevance
rubric (0-3), and whether it is a substantive page or thin, spam or junk (0-2). The pool -
Staan's results plus the LTR's top 30, without MMR - is then sorted by

    relevance + 0.5 x quality - 0.1 x Staan's position (10 if Staan didn't return it)
              - 0.5 if Staan didn't return it

and the top ten are served. Every weight was tuned on the 849 training queries' serving pool
(pass-3 NDCG@10), never on the en-gb evaluation queries. Judged holistically it beats
Staan-first, the previous best ordering, by a wide margin. The experiments and their write-up
are on the archived branch of mwmbl/mwmbl#462 (scripts/combined_ltr_labels/jev_composite.py,
mwmbl/rankeval/combined-holistic-eval.md).

Leaning on Staan's order is deliberate: every learned ordering that overrode Staan's top
results lost to it holistically, even where it gained on per-page NDCG. MMR is not applied,
since judged holistically it made no difference and it demotes Staan's same-site results.

If Jev is unconfigured, fails or is slow, the request falls back to the LTR + MMR ordering
served before this, so Jev being down costs quality, never results.
"""

import time
from logging import getLogger

import numpy as np
import requests
from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.mmr_rank import mmr_rerank
from mwmbl.tinysearchengine.rank import Ranker, Retrieval, find_blacklisted_urls
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE

logger = getLogger(__name__)

NUM_RESULTS = 10
# How many of the LTR's results join Staan's in the pool Jev scores.
NUM_LTR_CANDIDATES = 30
# The Staan position given to a candidate Staan didn't return: one past its last result.
NOT_IN_STAAN = 10

RELEVANCE_WEIGHT = 1.0
QUALITY_WEIGHT = 0.5
STAAN_POSITION_WEIGHT = 0.1
INDEX_PENALTY = 0.5

SEARCHER = "a web searcher in the United Kingdom"

# The wording is the experiment's exactly: the weights were tuned on Jev's answers to these
# questions, so changing a word means re-tuning them.
RELEVANCE_QUESTION = "How well does the search result `result` satisfy `query`, for `searcher`?"
RELEVANCE_CRITERIA = [
    "Irrelevant: off-topic, wrong entity, spam, or broken.",
    "Marginal: on-topic-ish but thin or tangential, the wrong sense of an ambiguous query, an SEO or "
    "doorway page, or a page for another country where the answer depends on the country.",
    "Good: relevant and useful, but partial, secondary, or less authoritative.",
    "Excellent: directly satisfies what the searcher most likely wants - the official site for a "
    "navigational query, a thorough direct answer to a question, an authoritative page on exactly "
    "that entity, the right local or transactional page, a current report for a news query.",
]
QUALITY_QUESTION = "Is the search result `result` a substantive page, judging by its title, URL and snippet?"
QUALITY_CRITERIA = [
    "Junk: spam, a content farm, a doorway or SEO page, a scraped or auto-generated page, a page "
    "selling something unrelated, or a broken, empty, login or error page.",
    "Thin: little real content - a stub, a bare listing or directory page, a tag or search page, "
    "or a page that only repeats the query's words.",
    "Substantive: a real page with real content, or the real site or service it names.",
]
QUESTIONS = {
    "r": (RELEVANCE_QUESTION, RELEVANCE_CRITERIA),
    "q": (QUALITY_QUESTION, QUALITY_CRITERIA),
}

# One session for the process, so its pooled connections are reused: Jev is on Combined
# Search's critical path, after Staan.
session = requests.Session()


def jev_request(query: str, documents: list[Document]) -> dict:
    questions = {
        f"{prefix}{i}": {
            "type": "score",
            "instructions": {
                "result": {"title": document.title, "url": document.url, "snippet": document.extract or ""},
                "question": question,
            },
            "criteria": criteria,
        }
        for i, document in enumerate(documents)
        for prefix, (question, criteria) in QUESTIONS.items()
    }
    return {"model": settings.JEV_MODEL, "state": {"query": query, "searcher": SEARCHER}, "questions": questions}


def get_jev_scores(query: str, documents: list[Document]) -> np.ndarray:
    """Jev's (relevance, quality) for each document, one row each. Raises on any failure."""
    response = session.post(
        settings.JEV_URL,
        json=jev_request(query, documents),
        headers={"Authorization": f"Bearer {settings.JEV_API_KEY}"},
        timeout=settings.JEV_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    answers = response.json()["answers"]
    return np.array([[answers[f"{prefix}{i}"]["score"] for prefix in QUESTIONS] for i in range(len(documents))])


def staan_position(document: Document) -> int:
    # A Staan result's score encodes the rank Staan gave it (see staan_score), which counts
    # Staan's results before the blacklist filter, exactly as the experiment did.
    if document.source == DocumentSource.STAAN:
        return round(STAAN_TOP_SCORE - document.score)
    return NOT_IN_STAAN


def composite_order(pool: list[Document], jev_scores: np.ndarray) -> list[Document]:
    positions = np.array([staan_position(document) for document in pool], dtype=float)
    from_index = positions == NOT_IN_STAAN
    composite = (
        RELEVANCE_WEIGHT * jev_scores[:, 0]
        + QUALITY_WEIGHT * jev_scores[:, 1]
        - STAAN_POSITION_WEIGHT * positions
        - INDEX_PENALTY * from_index
    )
    # Stable, so ties keep the pool's order: Staan's results first, in Staan's order.
    order = np.argsort(-composite, kind="stable")
    return [pool[i] for i in order]


def candidate_pool(staan_results: list[Document], ranked: list[Document]) -> list[Document]:
    """Staan's results, then the LTR's top results, each URL once.

    Staan's come from Staan's own list rather than the LTR's output, so a Staan result the
    model scored below zero still gets judged, as in the experiment. They still go through
    the blacklist, which the LTR's output already has.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results) if settings.BLACKLIST_FILTER_AT_RETRIEVAL else set()
    staan_kept = [document for document in staan_results if document.url not in blacklisted_urls]
    by_url = {document.url: document for document in staan_kept + ranked[:NUM_LTR_CANDIDATES]}
    return list(by_url.values())


class JevRanker:
    """Decorator that orders a CombinedLTRRanker's pool with Jev composite + Staan rank.

    The wrapped ranker must not be wrapped in MMRRanker: Jev scores the LTR's own order, and
    MMR is only applied on the fallback path.
    """

    def __init__(self, ranker: Ranker):
        self.ranker = ranker

    def search(self, s: str, additional_results: list[Document], use_external_search: bool = True) -> list[Document]:
        ranked = self.ranker.search(s, additional_results, use_external_search)
        return self.rerank(s, additional_results, ranked)

    def retrieve(self, q: str) -> Retrieval:
        return self.ranker.retrieve(q)

    def search_retrieved(self, retrieval: Retrieval, additional_results: list[Document]) -> list[Document]:
        ranked = self.ranker.search_retrieved(retrieval, additional_results)
        return self.rerank(retrieval.query, additional_results, ranked)

    def complete(self, q: str):
        return self.ranker.complete(q)

    def get_raw_results(self, query: str):
        return self.ranker.get_raw_results(query)

    def rerank(self, query: str, additional_results: list[Document], ranked: list[Document]) -> list[Document]:
        staan_results = [document for document in additional_results if document.source == DocumentSource.STAAN]
        pool = candidate_pool(staan_results, ranked)
        if not pool:
            return []

        if not settings.JEV_API_KEY:
            logger.warning("JEV_API_KEY is not configured")
            return mmr_rerank(ranked)

        start = time.monotonic()
        try:
            jev_scores = get_jev_scores(query, pool)
        except Exception as e:
            # Only the type, never the message or body: both can embed the user's query.
            logger.warning("Jev failed, serving the LTR + MMR ordering: %s", type(e).__name__)
            return mmr_rerank(ranked)
        logger.info("Jev scored %d candidates in %.2fs", len(pool), time.monotonic() - start)

        return composite_order(pool, jev_scores)[:NUM_RESULTS]
