"""
Evaluate Super Search as a *fallback* for standard search.

Super Search is a heavy pipeline (extra source fan-out, page crawling, outbound-link
following) that, on average, trails plain standard search on the gold set
(see ``SUPER_SEARCH_ADD_SOURCES_FINDINGS.md``). But it was never meant to run on
every query — it is intended to rescue queries where the *standard* Mwmbl search
comes up short. The prior evaluations applied it to all queries and so measured the
wrong thing.

This harness models the intended usage: serve standard search, and only fall back
to Super Search when standard returns ``<= threshold`` results. Standard search is
the full served pipeline (LTR + MMR + up to 3 Wikipedia results) over the **remote
production index** (``api.mwmbl.org``, like ``evaluate_remote.py``) by default — the
tiny local dev index returns ``<= 3`` results for almost every query, which would
make the fallback fire on nearly everything and the experiment meaningless. With the
production index the fallback fires only on genuine standard-search failures, which
is the whole point. Pass ``--local`` to score against the local index instead.

Caveat: when the fallback fires, Super Search's own ``mwmbl_index`` source still
queries the *local* index (that is what the offline pipeline has loaded), so Super
Search's in-index contribution is understated. But the fallback only fires when the
*production* index has already come up short, so on those queries the local index
would almost certainly fail too — the value Super Search adds there is its extra
sources, crawling and link-following, which the offline pipeline does run for real.

When the fallback fires we *replace* the ranking with Super Search's, rather than
merging: Super Search's pool already contains the mwmbl-index source and the same
Wikipedia augmentation, so its ranking subsumes standard's inputs. This matches what
production would actually show (swap the SERP for the Super Search SERP).

For each threshold we report overall NDCG (directly comparable to the standard and
always-Super-Search baselines on the same sampled queries) plus the diagnostic that
matters: how often the fallback fires, and whether Super Search beats standard *on
the subset where it fires* — that subset is the entire case for the feature.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \\
        uv run python -m mwmbl.rankeval.evaluation.evaluate_fallback --fraction 0.05
"""
import os
from argparse import ArgumentParser

import django
import numpy as np
import pandas as pd
from scipy.stats import sem
from sklearn.metrics import ndcg_score

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

import mwmbl.rankeval.evaluation.evaluate_super_search as ss_module  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate import (  # noqa: E402
    CLICK_PROPORTIONS, NUM_RESULTS_FOR_EVAL, RankingModel)
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_super_search import SuperSearchRankingModel  # noqa: E402
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex  # noqa: E402
from mwmbl.rankeval.paths import (  # noqa: E402
    RANKINGS_DATASET_TEST_PATH, RANKINGS_DATASET_TRAIN_PATH, RUST_MODEL_PATH)
from mwmbl.tinysearchengine.ltr import RustXGBPipeline  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402


def _standard_model(use_local: bool) -> MwmblRankingModel:
    """Standard search: LTR + MMR + 3 wiki results, over remote or local index.

    Remote (default) hits the production index at ``api.mwmbl.org`` so the fallback
    fires at a realistic rate; ``--local`` reuses the dev index from ``search_setup``.
    """
    if use_local:
        from mwmbl.search_setup import ranker  # local index + LTR + MMR + wiki
        return MwmblRankingModel(ranker)
    model = RustXGBPipeline.from_model_path(str(RUST_MODEL_PATH))
    ranker = MMRRanker(LTRRanker(RemoteIndex(), DummyCompleter(), model, True, 3))
    return MwmblRankingModel(ranker)


class FallbackRankingModel(RankingModel):
    """Serve ``primary``; fall back to ``fallback`` only when primary is starved.

    If the primary model returns ``<= threshold`` results for a query, the fallback
    model's ranking replaces it; otherwise the primary ranking is served unchanged.
    Reusable for any pair of ``RankingModel`` s, not just standard vs Super Search.
    """

    def __init__(self, primary: RankingModel, fallback: RankingModel, threshold: int):
        self.primary = primary
        self.fallback = fallback
        self.threshold = threshold

    def predict(self, query: str) -> list[str]:
        primary_results = self.primary.predict(query)
        if len(primary_results) <= self.threshold:
            return self.fallback.predict(query)
        return primary_results


def query_ndcg(predicted_urls: list[str], gold_scores: dict[str, float]) -> float:
    """NDCG@10 of a predicted ranking against gold, matching ``evaluate.evaluate``.

    ``gold_scores`` maps each gold URL to its click-proportion relevance weight.
    """
    top_urls = predicted_urls[:NUM_RESULTS_FOR_EVAL]
    y_true = [gold_scores.get(url, 0.0) for url in top_urls] + [0.0] * (10 - len(top_urls))
    y_predicted = list(range(NUM_RESULTS_FOR_EVAL, 0, -1))
    return ndcg_score([y_true], [y_predicted])


def _mean_sem(values: list[float]) -> str:
    if not values:
        return "    n/a"
    return f"{np.mean(values):.4f} ± {sem(values):.4f}" if len(values) > 1 else f"{np.mean(values):.4f}"


def run():
    parser = ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.05,
                        help="Fraction of gold queries to sample (Super Search is slow).")
    parser.add_argument("--thresholds", type=int, nargs="+", default=[1, 3, 5],
                        help="Fallback when standard returns <= threshold results.")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    parser.add_argument("--local", action="store_true",
                        help="Use the local dev index for standard search instead of "
                             "the remote production index (fallback then fires on almost "
                             "every query — for debugging only).")
    parser.add_argument("--fired-only", action="store_true",
                        help="Only run Super Search on queries where the fallback fires "
                             "(faster, but omits the always-Super-Search comparison arm).")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the Super Search doc-pool cache before evaluating.")
    args = parser.parse_args()

    if args.clear_cache:
        ss_module.memory.clear(warn=False)

    print("Standard search index:", "local dev" if args.local else "remote (api.mwmbl.org)")
    standard = _standard_model(use_local=args.local)
    super_search = SuperSearchRankingModel()

    path = RANKINGS_DATASET_TRAIN_PATH if args.train else RANKINGS_DATASET_TEST_PATH
    print("Evaluating against dataset", path)
    dataset = pd.read_csv(path)

    queries = dataset["query"].unique()
    rng = np.random.default_rng(42)  # same seed/sampling as evaluate.evaluate
    if args.fraction < 1.0:
        num_queries = int(args.fraction * len(queries))
        sampled = set(rng.choice(queries, num_queries, replace=False))
    else:
        sampled = set(queries)
    print("Num queries", len(sampled))

    # Per-query: gold scores, standard NDCG + result count, and Super Search NDCG.
    # By default Super Search is run on every sampled query so we can report the
    # always-Super-Search arm (directly comparable to compare_search_modes.py); with
    # --fired-only it is computed just for queries where the fallback fires.
    max_threshold = max(args.thresholds)
    standard_ndcg: dict[str, float] = {}
    standard_count: dict[str, int] = {}
    super_ndcg: dict[str, float] = {}

    for query, rankings in dataset.groupby("query"):
        if query not in sampled:
            continue
        top_ranked = rankings[["url"]].iloc[:NUM_RESULTS_FOR_EVAL].copy()
        top_ranked["score"] = CLICK_PROPORTIONS[:len(top_ranked)]
        gold_scores = top_ranked.set_index("url")["score"].to_dict()

        std_results = standard.predict(query)
        standard_count[query] = len(std_results)
        standard_ndcg[query] = query_ndcg(std_results, gold_scores)

        if not args.fired_only or len(std_results) <= max_threshold:
            super_ndcg[query] = query_ndcg(super_search.predict(query), gold_scores)

    # Headline arms over all sampled queries.
    print(f"\n{'=' * 78}")
    print("Standard search (always):     NDCG =", _mean_sem(list(standard_ndcg.values())))
    if not args.fired_only:
        print("Super Search  (always):       NDCG =", _mean_sem(list(super_ndcg.values())))
    print(f"{'=' * 78}")

    print(f"\n{'thresh':>6}  {'fired':>12}  {'fallback NDCG (all)':>22}  "
          f"{'fired-subset std':>17}  {'fired-subset super':>18}  {'Δ on fired':>11}")
    for threshold in sorted(args.thresholds):
        fired = [q for q, c in standard_count.items() if c <= threshold]
        fallback_all = [
            super_ndcg[q] if standard_count[q] <= threshold else standard_ndcg[q]
            for q in standard_ndcg
        ]
        fired_std = [standard_ndcg[q] for q in fired]
        fired_super = [super_ndcg[q] for q in fired]
        delta = (np.mean(fired_super) - np.mean(fired_std)) if fired else float("nan")
        n_total = len(standard_ndcg)
        print(f"{threshold:>6}  {len(fired):>5}/{n_total:<6}  {_mean_sem(fallback_all):>22}  "
              f"{_mean_sem(fired_std):>17}  {_mean_sem(fired_super):>18}  {delta:>+11.4f}")

    print("\nfired-subset Δ = mean(Super Search NDCG − standard NDCG) on queries where the "
          "fallback fires;\npositive means falling back to Super Search helps where standard "
          "is starved.")


if __name__ == "__main__":
    run()
