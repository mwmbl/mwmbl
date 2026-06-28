#!/usr/bin/env python3
"""Targeted NDCG comparison for the newly-added Super Search sources.

The full-sample compare_search_modes dilutes the effect of a few high-value
sources across thousands of unrelated queries. This script instead evaluates only
the *in-coverage* test queries — those whose gold results include a URL on a new
source's domain — where source selection can actually change the ranking. For each
such query it scores three arms (standard / SS baseline / SS + new sources) with
the same gold→NDCG method as rankeval.evaluation.evaluate.

Run:
  DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
      uv run python scripts/super_search_new_sources_eval.py --max-queries 80
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import django
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.conf import settings  # noqa: E402
from sklearn.metrics import ndcg_score  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate import CLICK_PROPORTIONS, NUM_RESULTS_FOR_EVAL  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_ranker import MwmblRankingModel  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_super_search import SuperSearchRankingModel  # noqa: E402
from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH  # noqa: E402
from mwmbl.search_setup import ranker  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.registry import get_meta  # noqa: E402

NEW_SOURCES = ["www_gov_uk", "imdb"]


def _registrable(host: str) -> str:
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else host


def _host(url: str) -> str:
    try:
        return urlparse(str(url)).netloc.lower().split("@")[-1].split(":")[0]
    except ValueError:
        return ""


def _ndcg(predicted_urls: list[str], gold_scores: dict[str, float]) -> float:
    top = predicted_urls[:NUM_RESULTS_FOR_EVAL]
    y_true = [gold_scores.get(u, 0.0) for u in top] + [0.0] * (NUM_RESULTS_FOR_EVAL - len(top))
    y_pred = list(range(NUM_RESULTS_FOR_EVAL, 0, -1))
    return ndcg_score([y_true], [y_pred])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-queries", type=int, default=80)
    args = parser.parse_args()

    df = pd.read_csv(RANKINGS_DATASET_TEST_PATH)
    new_domains = {_registrable(get_meta(n).domain.lower()) for n in NEW_SOURCES}

    # In-coverage queries: a gold URL of theirs sits on one of the new domains.
    covered = {q for q, u in zip(df["query"], df["url"])
               if _registrable(_host(u)) in new_domains}
    queries = sorted(covered)
    rng = np.random.default_rng(0)
    rng.shuffle(queries)
    queries = queries[:args.max_queries]
    print(f"{len(covered)} in-coverage test queries; evaluating {len(queries)}")

    # Gold score dicts per query (same construction as evaluate.py).
    gold = {}
    for q in queries:
        rows = df[df["query"] == q][["url"]].iloc[:NUM_RESULTS_FOR_EVAL].copy()
        rows["score"] = CLICK_PROPORTIONS[:len(rows)]
        gold[q] = rows.set_index("url")["score"].to_dict()

    arms = [
        ("standard", lambda: MwmblRankingModel(ranker), None),
        ("ss-baseline", lambda: SuperSearchRankingModel(selection_key=""), []),
        ("ss+new", lambda: SuperSearchRankingModel(selection_key="+".join(NEW_SOURCES)), NEW_SOURCES),
    ]
    per_query = {name: {} for name, _, _ in arms}
    for name, make_model, force in arms:
        if force is not None:
            settings.SUPER_SEARCH_FORCE_INCLUDE = force
        model = make_model()
        print(f"\n--- arm {name} ---")
        for i, q in enumerate(queries):
            preds = model.predict(q)
            per_query[name][q] = _ndcg(preds, gold[q])
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(queries)}")
    settings.SUPER_SEARCH_FORCE_INCLUDE = []

    print("\n" + "=" * 60)
    print(f"{'arm':14} {'mean NDCG':>10}")
    for name, _, _ in arms:
        vals = list(per_query[name].values())
        print(f"{name:14} {np.mean(vals):>10.4f}")

    base = per_query["ss-baseline"]
    new = per_query["ss+new"]
    wins = sum(new[q] > base[q] + 1e-9 for q in queries)
    losses = sum(new[q] < base[q] - 1e-9 for q in queries)
    delta = np.mean([new[q] - base[q] for q in queries])
    print(f"\nss+new vs ss-baseline: mean Δ {delta:+.4f} | better {wins}, worse {losses}, "
          f"same {len(queries) - wins - losses}")
    movers = sorted(queries, key=lambda q: new[q] - base[q], reverse=True)
    print("\ntop NDCG gains (ss+new over baseline):")
    for q in movers[:8]:
        if new[q] - base[q] > 1e-9:
            print(f"  {new[q] - base[q]:+.3f}  {q!r}")


if __name__ == "__main__":
    main()
