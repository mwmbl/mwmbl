"""
Compare standard search vs Super Search v2 on the gold test set.

Both are scored against the same gold test split via ``rankeval.evaluation.evaluate``
on the same sampled queries (the evaluate module RNG is reseeded per model).

Fairness note — the standard-search baseline uses the *same* local mwmbl index,
retrained LTR model, MMR and Wikipedia augmentation that Super Search's
``mwmbl_index`` source uses (``search_setup.ranker``). So the only difference
being measured is what Super Search *adds*: its extra sources (HN, GitHub, ArXiv,
PyPI, Stack Exchange, recipes), page crawling and outbound-link following. A
remote-index baseline (``evaluate_remote.py``) would be confounded by index size,
because Super Search's mwmbl source queries the local index, not the production one.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
        uv run python -m mwmbl.rankeval.evaluation.compare_search_modes --fraction 0.05
"""
import os
from argparse import ArgumentParser

import django
import numpy as np

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

import mwmbl.rankeval.evaluation.evaluate as evaluate_module  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate import evaluate  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_ranker import MwmblRankingModel  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_super_search import SuperSearchRankingModel  # noqa: E402
from mwmbl.search_setup import ranker  # noqa: E402  (local mwmbl index + LTR + MMR + wiki)


def run():
    parser = ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.05,
                        help="Fraction of gold test queries to sample.")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    args = parser.parse_args()

    models = [
        ("standard search (local index + wiki)", MwmblRankingModel(ranker)),
        ("super search v2 (cosine baseline)", SuperSearchRankingModel()),
    ]
    for label, model in models:
        print(f"\n{'=' * 70}\nEvaluating {label}\n{'=' * 70}")
        # Reseed so both modes are scored on the same sampled query subset.
        evaluate_module.random = np.random.default_rng(42)
        evaluate(model, fraction=args.fraction, use_test=not args.train)


if __name__ == "__main__":
    run()
