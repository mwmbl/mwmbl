"""
Evaluate the Super Search pipeline as a RankingModel against the gold dataset.

Runs the real Super Search ranking core offline: select sources (cosine baseline
by default — see ``SUPER_SEARCH_USE_BANDIT``), fan out to them, re-rank the union
with the LTR model, crawl promoted pages, follow outbound links, and MMR-diversify
into a final ranking. Only the SSE/auth/quota/indexing/reward machinery of the
HTTP endpoint is skipped; the ranking logic is exactly what production serves.

The expensive part — source fan-out, page crawling and link-following — is cached
per query (joblib ``Memory`` in ``devdata/super-search-eval-cache``). The cache
stores the collected document pool *before* ranking, so the LTR/MMR ranking step
is re-run fresh every time: iterate on ranking code and re-run without re-paying
the network cost. The cache is keyed by query and by the collection code; pass
``--clear-cache`` to force a full re-fetch (e.g. after changing source adapters,
promotion or crawl logic).

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
        uv run python -m mwmbl.rankeval.evaluation.evaluate_super_search --fraction 0.02
"""
import asyncio
import os
from argparse import ArgumentParser
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

# Imported after django.setup(): super_search pulls in search_setup (index + model).
from django.conf import settings  # noqa: E402
from joblib import Memory  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate  # noqa: E402
from mwmbl.rankeval.paths import DATA_DIR  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.super_search import _emit_final_results, _run_pipeline  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext  # noqa: E402


memory = Memory(location=str(DATA_DIR / "super-search-eval-cache"), verbose=0)


async def _noop_emit(event_type, data):
    """Discard the SSE events the pipeline emits; we only want the documents."""


def _doc_to_dict(doc: Document) -> dict:
    return {"title": doc.title, "url": doc.url, "extract": doc.extract,
            "score": doc.score, "term": doc.term, "state": doc.state}


def _dict_to_doc(d: dict) -> Document:
    return Document(title=d["title"], url=d["url"], extract=d["extract"],
                    score=d["score"], term=d["term"], state=d["state"])


async def _collect(query: str) -> list[Document]:
    """Run the network pipeline (sources + crawl + links) and return the doc pool.

    The collected pool depends only on source selection, heuristic promotion and
    crawling — not on the LTR/MMR ranking — so it is safe to cache across ranking
    changes. ``_run_pipeline`` also produces interim rankings internally; we
    ignore those and keep only the accumulated documents.
    """
    all_docs: list[Document] = []
    last_results_key: list = [None]
    lock = asyncio.Lock()
    ctx = SelectionContext()
    try:
        await asyncio.wait_for(
            _run_pipeline(query, _noop_emit, all_docs, last_results_key, lock, ctx),
            timeout=settings.SUPER_SEARCH_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        pass
    return all_docs


@memory.cache
def _collect_docs(query: str, selection_key: str = "") -> list[dict]:
    """Cached: the collected document pool for a query, as plain dicts.

    ``selection_key`` is part of the cache key only: the collected pool depends on
    which sources are queried (source selection is read from settings at call
    time), so callers that change selection — e.g. force-including extra sources —
    must pass a distinct key to avoid colliding with another config's cached pool.
    """
    return [_doc_to_dict(d) for d in asyncio.run(_collect(query))]


async def _rank(query: str, docs: list[Document]) -> list[str]:
    """Run the final LTR + MMR ranking over a document pool (cheap, not cached)."""
    last_results_key: list = [None]
    lock = asyncio.Lock()
    await _emit_final_results(query, docs, _noop_emit, last_results_key, lock)
    return list(last_results_key[0] or ())


class SuperSearchRankingModel(RankingModel):
    def __init__(self, selection_key: str = ""):
        # Distinguishes cached doc pools when source selection differs between runs
        # (e.g. force-including extra sources). Set settings to match before use.
        self.selection_key = selection_key

    def predict(self, query: str) -> list[str]:
        docs = [_dict_to_doc(d) for d in _collect_docs(query, self.selection_key)]
        return asyncio.run(_rank(query, docs))


def run():
    parser = ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.02,
                        help="Fraction of gold queries to sample (collection is slow).")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the cached document pools before evaluating.")
    args = parser.parse_args()

    if args.clear_cache:
        memory.clear(warn=False)
    evaluate(SuperSearchRankingModel(), fraction=args.fraction, use_test=not args.train)


if __name__ == "__main__":
    run()
