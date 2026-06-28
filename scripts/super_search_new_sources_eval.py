#!/usr/bin/env python3
"""Targeted NDCG comparison for the newly-added Super Search sources.

The full-sample compare_search_modes dilutes the effect of a few high-value
sources across thousands of unrelated queries. This script instead evaluates only
the *in-coverage* test queries — those whose gold results include a URL on a new
source's domain — where source selection can actually change the ranking. For each
such query it scores three arms (standard / SS baseline / SS + new sources) with
the same gold→NDCG method as rankeval.evaluation.evaluate.

It also evaluates Super Search as a *fallback*: Super Search is only invoked when
standard search comes up short (returns ``<= --fallback-threshold`` results),
otherwise the standard ranking is kept. This models the intended production usage —
Super Search rescues queries where standard search fails — rather than replacing
every ranking. The fallback arms are derived post-hoc from the per-query results of
the other arms (``fallback@n`` for a query = the Super Search NDCG when standard
returns ``<= n`` results, else the standard NDCG), so they add no extra model runs.

Standard search uses the **remote** production index (``api.mwmbl.org``) by default,
not the tiny local dev index: the gate counts standard results, and the local index
returns ``<= 3`` for almost every in-coverage query (their answers live on external
domains it does not hold), which would make the fallback fire on everything. Pass
``--standard-index local`` to reproduce the original local-index standard arm.

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
from mwmbl.rankeval.evaluation.evaluate_fallback import _standard_model  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_super_search import SuperSearchRankingModel  # noqa: E402
from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH  # noqa: E402
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
    parser.add_argument("--standard-index", choices=["remote", "local"], default="remote",
                        help="Index for the standard-search arm and the fallback gate "
                             "(default remote = api.mwmbl.org production index).")
    parser.add_argument("--fallback-thresholds", type=int, nargs="+", default=[1, 3, 5],
                        help="Fall back to Super Search when standard returns <= threshold "
                             "results. Evaluated post-hoc for each value.")
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

    print(f"standard-search index: {args.standard_index}")
    arms = [
        ("standard", lambda: _standard_model(use_local=args.standard_index == "local"), None),
        ("ss-baseline", lambda: SuperSearchRankingModel(selection_key=""), []),
        ("ss+new", lambda: SuperSearchRankingModel(selection_key="+".join(NEW_SOURCES)), NEW_SOURCES),
    ]
    per_query = {name: {} for name, _, _ in arms}
    standard_count = {}  # number of results standard search returned (the fallback gate)
    for name, make_model, force in arms:
        if force is not None:
            settings.SUPER_SEARCH_FORCE_INCLUDE = force
        model = make_model()
        print(f"\n--- arm {name} ---")
        for i, q in enumerate(queries):
            preds = model.predict(q)
            per_query[name][q] = _ndcg(preds, gold[q])
            if name == "standard":
                standard_count[q] = len(preds)
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(queries)}")
    settings.SUPER_SEARCH_FORCE_INCLUDE = []

    # Fallback arms, derived post-hoc: keep standard unless it returned <= n results,
    # in which case fall back to the corresponding always-Super-Search arm.
    for n in sorted(args.fallback_thresholds):
        for ss_name in ("ss-baseline", "ss+new"):
            per_query[f"fallback@{n}/{ss_name}"] = {
                q: (per_query[ss_name][q] if standard_count[q] <= n else per_query["standard"][q])
                for q in queries
            }

    print("\n" + "=" * 60)
    print(f"{'arm':24} {'mean NDCG':>10}")
    for name in per_query:
        print(f"{name:24} {np.mean(list(per_query[name].values())):>10.4f}")

    print(f"\nfallback fire rate (standard <= n results), of {len(queries)} queries:")
    for n in sorted(args.fallback_thresholds):
        fired = sum(c <= n for c in standard_count.values())
        print(f"  n={n}: {fired:3d} fired ({fired / len(queries):.0%})")

    base = per_query["ss-baseline"]
    new = per_query["ss+new"]
    wins = sum(new[q] > base[q] + 1e-9 for q in queries)
    losses = sum(new[q] < base[q] - 1e-9 for q in queries)
    delta = np.mean([new[q] - base[q] for q in queries])
    print(f"\nss+new vs ss-baseline: mean Δ {delta:+.4f} | better {wins}, worse {losses}, "
          f"same {len(queries) - wins - losses}")

    # The headline fallback question: does gating Super Search behind standard-search
    # failure beat serving standard search alone, on this in-coverage subset?
    std = per_query["standard"]
    print("\nfallback vs standard-always (paired, same queries):")
    for n in sorted(args.fallback_thresholds):
        for ss_name in ("ss-baseline", "ss+new"):
            fb = per_query[f"fallback@{n}/{ss_name}"]
            d = np.mean([fb[q] - std[q] for q in queries])
            w = sum(fb[q] > std[q] + 1e-9 for q in queries)
            l = sum(fb[q] < std[q] - 1e-9 for q in queries)
            print(f"  fallback@{n}/{ss_name:11} mean Δ {d:+.4f} | better {w}, worse {l}")

    # Why the fallback nets ~0: show, for the queries it fires on, whether Super
    # Search actually rescues them. If standard and Super Search both score ~0 on the
    # starved queries, the result-count gate is selecting queries SS cannot help.
    nmax = max(args.fallback_thresholds)
    fired = sorted([q for q in queries if standard_count[q] <= nmax],
                   key=lambda q: standard_count[q])
    print(f"\nfired queries at n={nmax} (standard count, NDCG: standard / ss-baseline / ss+new):")
    for q in fired:
        print(f"  cnt={standard_count[q]:2d}  {std[q]:.3f} / {base[q]:.3f} / {new[q]:.3f}  {q!r}")


if __name__ == "__main__":
    main()
