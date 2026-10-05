"""Evaluate the Combined Search ranking core as a RankingModel against the gold dataset.

Runs what /api/v2/combined-search/ runs, minus the HTTP layer: pool the Mwmbl index and
Staan, rank the union with the combined LTR model, diversify. Only auth, quota and
response formatting are skipped, so the ranking measured here is the ranking served.

Not to be confused with evaluate_combined.py, which combines two ranking *models* (Wikipedia
and Mwmbl) and predates this endpoint.

Staan is a network call, cached through the external results cache, so a gold-set run pays
for it once per query and is cheap to repeat while iterating on the ranking. Pass
``--no-staan`` to measure what Staan is actually contributing.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
        uv run python -m mwmbl.rankeval.evaluation.evaluate_combined_search --fraction 0.05
"""

import os
from argparse import ArgumentParser

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

# Imported after django.setup(): search_setup opens the index and loads the models.
from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate  # noqa: E402
from mwmbl.search_setup import combined_ranker  # noqa: E402
from mwmbl.tinysearchengine.staan import get_staan_results  # noqa: E402


class CombinedSearchRankingModel(RankingModel):
    """The endpoint's ranking core: the index and Staan pooled, one model, MMR.

    include_staan=False is the ablation arm - the same pool minus Staan, so the difference
    between the two arms is what Staan contributes and nothing else.
    """

    def __init__(self, ranker=combined_ranker, include_staan: bool = True):
        self.ranker = ranker
        self.include_staan = include_staan

    def predict(self, query: str) -> list[str]:
        additional = get_staan_results(query) if self.include_staan else []
        results = self.ranker.search(query, additional, False)
        return [result.url for result in results]


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--fraction", type=float, default=0.05, help="Fraction of gold queries to sample.")
    parser.add_argument("--train", action="store_true", help="Evaluate on the train split instead of test.")
    parser.add_argument("--no-staan", action="store_true", help="Ablation: the index only.")
    args = parser.parse_args()

    model = CombinedSearchRankingModel(include_staan=not args.no_staan)
    evaluate(model, fraction=args.fraction, use_test=not args.train)


if __name__ == "__main__":
    run()
