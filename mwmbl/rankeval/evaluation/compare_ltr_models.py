"""
Compare two (or more) LTR models end-to-end on the gold test set.

Each model is plugged into the production ranking stack
(``MMRRanker(LTRRanker(RemoteIndex(), ...))``) and scored by the NDCG harness in
``rankeval.evaluation.evaluate`` against the held-out test split. Use this to
check whether retraining the LTR model on a new dataset actually improves
ranking quality versus the current model.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python \
        -m mwmbl.rankeval.evaluation.compare_ltr_models \
        --model current=devdata/rankeval-2026-04/model-current.xgb \
        --model retrained=devdata/rankeval-2026-04/model.xgb \
        --fraction 0.1
"""
import os
from argparse import ArgumentParser

import django
import numpy as np

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

import mwmbl.rankeval.evaluation.evaluate as evaluate_module  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate import evaluate  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker


def model_from_path(path: str) -> MwmblRankingModel:
    model = RustXGBPipeline.from_model_path(path)
    ranker = MMRRanker(LTRRanker(RemoteIndex(), DummyCompleter(), model, True, 3))
    return MwmblRankingModel(ranker)


def run():
    parser = ArgumentParser()
    parser.add_argument("--model", action="append", required=True, metavar="LABEL=PATH",
                        help="A model to evaluate, e.g. current=path/to/model.xgb. Repeatable.")
    parser.add_argument("--fraction", type=float, default=0.1,
                        help="Fraction of gold test queries to sample.")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    args = parser.parse_args()

    labelled = [spec.split("=", 1) for spec in args.model]
    for label, path in labelled:
        print(f"\n{'=' * 70}\nEvaluating model {label!r} from {path}\n{'=' * 70}")
        # Reseed evaluate's module-level RNG so every model is scored on the
        # exact same sampled query subset (a fair head-to-head).
        evaluate_module.random = np.random.default_rng(42)
        evaluate(model_from_path(path), fraction=args.fraction, use_test=not args.train)


if __name__ == "__main__":
    run()
