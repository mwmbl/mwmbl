"""
Evaluate the Super Search pipeline as a RankingModel against the gold dataset.

Runs the real Super Search ranking core offline: select sources (cosine baseline
by default — see ``SUPER_SEARCH_USE_BANDIT``), fan out to them, re-rank the union
with the LTR model, crawl promoted pages, follow outbound links, and MMR-diversify
into a final ranking. Only the SSE/auth/quota/indexing/reward machinery of the
HTTP endpoint is skipped; the ranking logic is exactly what production serves.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
        uv run python -m mwmbl.rankeval.evaluation.evaluate_super_search --fraction 0.02

The pipeline crawls pages and follows links, so it is network-heavy; use
``--fraction`` to sample the gold queries.
"""
import asyncio
import os
from argparse import ArgumentParser

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

# Imported after django.setup(): super_search pulls in search_setup (index + model).
from django.conf import settings  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.super_search import _emit_final_results, _run_pipeline  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext  # noqa: E402


async def _noop_emit(event_type, data):
    """Discard the SSE events the pipeline emits; we only want the final ranking."""


async def _super_search(query: str) -> list[str]:
    """Run the pipeline for one query and return the final ranked URLs."""
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
    # Emit one final ranking over whatever was collected before the deadline.
    await _emit_final_results(query, all_docs, _noop_emit, last_results_key, lock)
    return list(last_results_key[0] or ())


class SuperSearchRankingModel(RankingModel):
    def predict(self, query: str) -> list[str]:
        return asyncio.run(_super_search(query))


def run():
    parser = ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.02,
                        help="Fraction of gold queries to sample (pipeline is slow).")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    args = parser.parse_args()
    evaluate(SuperSearchRankingModel(), fraction=args.fraction, use_test=not args.train)


if __name__ == "__main__":
    run()
