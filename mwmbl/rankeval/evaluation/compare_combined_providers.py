"""Compare Combined Search with Staan alone and with Brave, on the production index.

Two questions, one run:

1. Does Combined Search beat Staan on its own - i.e. do Mwmbl's results earn their place?
2. How does Combined Search compare with Brave, on quality and on latency?

The arms, each scored on the same sampled gold test queries:

- ``staan``: Staan's top ten in Staan's own order - what a client gets calling Staan direct.
- ``staan+wiki``: Staan and Wikipedia ranked by the combined model over an empty index, so
  the step from ``staan`` is the re-ranker and Wikipedia, and nothing from Mwmbl.
- ``combined``: the endpoint - production index + Staan + Wikipedia, combined model, MMR.
  The step from ``staan+wiki`` is exactly what Mwmbl's own results add.
- ``mwmbl``: the production index + Wikipedia under the combined model, no Staan.
- ``brave``: the Brave Search API's top ten, when ``BRAVE_SEARCH_API_KEY`` is set.

And the experiments on the combined arm, named by what they change:

- ``-nowiki``: Staan only, no Wikipedia - Staan already returns Wikipedia pages when they
  are relevant, so the separate Wikipedia results may only be adding noise.
- ``-keep``: Staan results are exempt from the model's majority-terms filter, which zeroes
  (and so drops) any candidate matching no more than half the query terms. Staan matches
  semantically, so a low term overlap says less about its results than about the index's.
- ``control-``: a Python retrain of the combined model on the same rows and parameters -
  the baseline for ``provider-``, confirming the retrain alone moves nothing.
- ``provider-``: that retrain with Staan's own ranking as features (``in_staan`` and
  ``staan_rank``; see ``mwmbl.rankeval.ltr.provider_features``).

The ``-keep`` and model experiments score with the Python booster over the Rust feature
matrix, which reproduces RustXGBPipeline.predict exactly before its filter.

The Mwmbl side uses RemoteIndex (api.mwmbl.org) rather than the local dev index, which is a
10 MB sample and would understate what the index contributes.

Two quality measures, because each has a bias the other lacks:

- **Gold NDCG** against scraped Google SERPs (``evaluate.gold_scores_for``). It rewards
  agreeing with Google, which favours any engine that resembles Google.
- **Judge NDCG@10**: every result in the union of the arms' top tens is scored blind by the
  local cross-encoder judge (``super_search_select.judge``), and each arm's list is scored
  against the ideal ordering of that pool. Nothing Google-shaped reaches it.

Every paired difference is reported with a bootstrap 95% interval over queries.

Latency is measured separately (``--latency N``) by calling each provider's API directly,
uncached, interleaved query by query so both see the same network conditions.

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \\
        uv run python -m mwmbl.rankeval.evaluation.compare_combined_providers --fraction 0.05
"""

import json
import os
import time
from argparse import ArgumentParser
from pathlib import Path

import django
import numpy as np
import pandas as pd
import requests
import xgboost as xgb
from joblib import Memory
from sklearn.metrics import ndcg_score

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.conf import settings  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate import NUM_RESULTS_FOR_EVAL, gold_scores_for  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter  # noqa: E402
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex  # noqa: E402
from mwmbl.rankeval.ltr.provider_features import (  # noqa: E402
    fails_term_filter,
    feature_matrix,
)
from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH  # noqa: E402
from mwmbl.search_setup import combined_ltr_model  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document, DocumentSource  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402
from mwmbl.tinysearchengine.rank import get_wiki_results  # noqa: E402
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE, get_staan_results  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text  # noqa: E402

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
# The free Brave plan allows one request per second.
BRAVE_MIN_INTERVAL_SECONDS = 1.1

OUTPUT_DIR = Path("devdata/combined_providers_eval")
CONTROL_MODEL_PATH = Path("devdata/rankeval-2026-04/model-combined-control.json")
PROVIDER_MODEL_PATH = Path("devdata/rankeval-2026-04/model-combined-provider.json")
NUM_BOOTSTRAP = 10_000

memory = Memory(location="devdata/cache")


class EmptyIndex:
    """An index with nothing in it, so the ranker sees only the additional results."""

    def retrieve(self, term: str) -> list[Document]:
        return []


class BoosterRanker(LTRRanker):
    """LTRRanker scored by a Python XGBoost booster, so the experiments can change what it sees.

    ``provider_features`` adds Staan's ranking as features, for a booster trained with them.
    ``keep_staan`` exempts Staan's results from the majority-terms filter.
    """

    def __init__(self, tiny_index, booster: xgb.Booster, provider_features: bool, keep_staan: bool):
        super().__init__(tiny_index, DummyCompleter(), model=None, include_wiki=False)
        self.booster = booster
        self.provider_features = provider_features
        self.keep_staan = keep_staan

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        query = " ".join(terms)
        records = [
            {
                "query": query,
                "url": page.url,
                "title": page.title if page.title is not None else "",
                "extract": page.extract if page.extract is not None else "",
                "score": page.score if page.score is not None else 0.0,
            }
            for page in results
        ]
        is_staan = np.array([page.source == DocumentSource.STAAN for page in results])
        staan_ranks = None
        if self.provider_features:
            # Each Staan result carries its rank in its score (see staan_score), which
            # survives the blacklist filter that would throw off counting positions.
            ranks = {
                page.url: round(STAAN_TOP_SCORE - page.score) for page in results if page.source == DocumentSource.STAAN
            }
            staan_ranks = [ranks] * len(records)

        features = feature_matrix(records, staan_ranks)
        predictions = self.booster.predict(xgb.DMatrix(features))
        dropped = fails_term_filter(features)
        if self.keep_staan:
            dropped &= ~is_staan

        kept = np.flatnonzero(~dropped)
        order = kept[np.argsort(predictions[kept])[::-1]]
        return [results[i] for i in order]


def _brave_request(query: str) -> requests.Response:
    response = requests.get(
        BRAVE_SEARCH_URL,
        params={"q": query, "count": NUM_RESULTS_FOR_EVAL},
        headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_SEARCH_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    return response


@memory.cache
def brave_results(query: str) -> list[dict]:
    time.sleep(BRAVE_MIN_INTERVAL_SECONDS)
    payload = _brave_request(query).json()
    web_results = payload.get("web", {}).get("results", [])
    return [
        {"url": result["url"], "title": result["title"], "extract": result.get("description", "")}
        for result in web_results[:NUM_RESULTS_FOR_EVAL]
    ]


def _as_dicts(documents: list[Document]) -> list[dict]:
    return [{"url": d.url, "title": d.title, "extract": d.extract} for d in documents[:NUM_RESULTS_FOR_EVAL]]


def build_arms(include_brave: bool):
    remote_index = RemoteIndex()
    remote_ranker = MMRRanker(LTRRanker(remote_index, DummyCompleter(), combined_ltr_model, include_wiki=False))
    empty_ranker = MMRRanker(LTRRanker(EmptyIndex(), DummyCompleter(), combined_ltr_model, include_wiki=False))

    combined_booster = xgb.Booster(model_file=str(settings.COMBINED_MODEL_PATH))
    control_booster = xgb.Booster(model_file=str(CONTROL_MODEL_PATH))
    provider_booster = xgb.Booster(model_file=str(PROVIDER_MODEL_PATH))

    def booster_arm(booster, provider_features, keep_staan, with_wiki):
        ranker = MMRRanker(BoosterRanker(remote_index, booster, provider_features, keep_staan))

        def arm(query, staan_docs, wiki_docs):
            additional = staan_docs + wiki_docs if with_wiki else staan_docs
            return _as_dicts(ranker.search(query, additional, False))

        return arm

    def staan(query, staan_docs, wiki_docs):
        return _as_dicts(staan_docs)

    def staan_wiki(query, staan_docs, wiki_docs):
        return _as_dicts(empty_ranker.search(query, staan_docs + wiki_docs, False))

    def combined(query, staan_docs, wiki_docs):
        return _as_dicts(remote_ranker.search(query, staan_docs + wiki_docs, False))

    def mwmbl(query, staan_docs, wiki_docs):
        return _as_dicts(remote_ranker.search(query, wiki_docs, False))

    def brave(query, staan_docs, wiki_docs):
        return brave_results(query)

    def combined_nowiki(query, staan_docs, wiki_docs):
        return _as_dicts(remote_ranker.search(query, staan_docs, False))

    arms = {
        "staan": staan,
        "staan+wiki": staan_wiki,
        "combined": combined,
        "mwmbl": mwmbl,
        "combined-nowiki": combined_nowiki,
        "combined-keep": booster_arm(combined_booster, False, keep_staan=True, with_wiki=True),
        "combined-nowiki-keep": booster_arm(combined_booster, False, keep_staan=True, with_wiki=False),
        "control-nowiki-keep": booster_arm(control_booster, False, keep_staan=True, with_wiki=False),
        "provider-nowiki": booster_arm(provider_booster, True, keep_staan=False, with_wiki=False),
        "provider-nowiki-keep": booster_arm(provider_booster, True, keep_staan=True, with_wiki=False),
    }
    if include_brave:
        arms["brave"] = brave
    return arms


def sample_queries(dataset: pd.DataFrame, fraction: float) -> set[str]:
    """The same sample evaluate.evaluate draws, so the numbers line up with earlier runs."""
    queries = dataset["query"].unique()
    num_queries = int(fraction * len(queries))
    rng = np.random.default_rng(42)
    return set(rng.choice(queries, num_queries, replace=False))


def gold_ndcg(urls: list[str], gold: dict[str, float]) -> float:
    y_true = [gold.get(url, 0.0) for url in urls] + [0.0] * (NUM_RESULTS_FOR_EVAL - len(urls))
    y_predicted = list(range(NUM_RESULTS_FOR_EVAL, 0, -1))
    return ndcg_score([y_true], [y_predicted])


def dcg(gains: list[float]) -> float:
    discounts = np.log2(np.arange(2, len(gains) + 2))
    return float(np.sum(np.array(gains) / discounts))


def judge_ndcg(urls: list[str], judged: dict[str, float]) -> float:
    ideal = sorted(judged.values(), reverse=True)[:NUM_RESULTS_FOR_EVAL]
    ideal_dcg = dcg(ideal)
    if ideal_dcg == 0.0:
        return 0.0
    return dcg([judged[url] for url in urls]) / ideal_dcg


def collect(dataset: pd.DataFrame, fraction: float, include_brave: bool, judge: Judge) -> list[dict]:
    arms = build_arms(include_brave)
    sampled = sample_queries(dataset, fraction)
    print(f"{len(sampled)} queries, arms: {', '.join(arms)}")

    rows = []
    for i, (query, rankings) in enumerate(dataset.groupby("query")):
        if query not in sampled:
            continue
        gold = gold_scores_for(rankings)
        staan_docs = get_staan_results(query)
        wiki_docs = get_wiki_results(query)
        lists = {name: arm(query, staan_docs, wiki_docs) for name, arm in arms.items()}

        pool = {result["url"]: result for results in lists.values() for result in results}
        pool_urls = list(pool)
        pool_texts = [doc_text(pool[url]["title"], pool[url]["extract"]) for url in pool_urls]
        judged = dict(zip(pool_urls, judge.score(query, pool_texts)))

        staan_urls = {d.url for d in staan_docs}
        wiki_urls = {d.url for d in wiki_docs}
        rows.append(
            {
                "query": query,
                "gold": gold,
                "staan_urls": sorted(staan_urls),
                "wiki_urls": sorted(wiki_urls),
                "lists": {name: [r["url"] for r in results] for name, results in lists.items()},
                "judged": judged,
            }
        )
        print(f"[{len(rows)}/{len(sampled)}] {query!r}: " + ", ".join(f"{n}={len(v)}" for n, v in lists.items()))
    return rows


def bootstrap_ci(differences: np.ndarray) -> tuple[float, float]:
    rng = np.random.default_rng(0)
    indices = rng.integers(0, len(differences), size=(NUM_BOOTSTRAP, len(differences)))
    means = differences[indices].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def per_query_metrics(rows: list[dict]) -> pd.DataFrame:
    records = []
    for row in rows:
        for arm, urls in row["lists"].items():
            judged_scores = [row["judged"][url] for url in urls]
            records.append(
                {
                    "query": row["query"],
                    "arm": arm,
                    "gold_ndcg": gold_ndcg(urls, row["gold"]),
                    "gold_recall": len(set(urls) & row["gold"].keys()) / NUM_RESULTS_FOR_EVAL,
                    "judge_ndcg": judge_ndcg(urls, row["judged"]),
                    "judge_mean": float(np.mean(judged_scores)) if judged_scores else 0.0,
                    "num_results": len(urls),
                }
            )
    return pd.DataFrame(records)


def mwmbl_contribution(rows: list[dict]) -> dict:
    """What Mwmbl's own results do inside the combined top ten, against what they displaced.

    A combined result is Mwmbl's when neither Staan nor Wikipedia returned it. The displaced
    results are Staan's and Wikipedia's that ranked in ``staan+wiki``'s top ten but not in
    ``combined``'s.
    """
    mwmbl_scores, displaced_scores, counts = [], [], []
    for row in rows:
        external = set(row["staan_urls"]) | set(row["wiki_urls"])
        combined = row["lists"]["combined"]
        mwmbl_in_combined = [url for url in combined if url not in external]
        displaced = [url for url in row["lists"]["staan+wiki"] if url not in combined]
        counts.append(len(mwmbl_in_combined))
        mwmbl_scores += [row["judged"][url] for url in mwmbl_in_combined]
        displaced_scores += [row["judged"][url] for url in displaced]
    return {
        "mean_mwmbl_results_in_top10": float(np.mean(counts)),
        "queries_with_any_mwmbl_result": float(np.mean(np.array(counts) > 0)),
        "mean_judge_mwmbl_results": float(np.mean(mwmbl_scores)) if mwmbl_scores else None,
        "mean_judge_displaced_results": float(np.mean(displaced_scores)) if displaced_scores else None,
        "num_mwmbl_results": len(mwmbl_scores),
        "num_displaced_results": len(displaced_scores),
    }


def report(rows: list[dict]) -> None:
    metrics = per_query_metrics(rows)
    metric_names = ["gold_ndcg", "gold_recall", "judge_ndcg", "judge_mean", "num_results"]
    summary = metrics.groupby("arm")[metric_names].agg(["mean", "sem"])
    print("\n=== Per-arm means (±SEM) ===")
    print(summary.round(3).to_string())

    wide = {name: metrics.pivot(index="query", columns="arm", values=name) for name in metric_names[:3]}
    arms = metrics["arm"].unique()
    comparisons = [("combined", "staan"), ("combined", "staan+wiki"), ("staan+wiki", "staan"), ("combined", "mwmbl")]
    experiments = [arm for arm in arms if arm.startswith(("combined-", "control-", "provider-"))]
    comparisons += [(arm, baseline) for arm in experiments for baseline in ("combined", "staan")]
    comparisons += [("provider-nowiki-keep", "control-nowiki-keep"), ("control-nowiki-keep", "combined-nowiki-keep")]
    if "brave" in arms:
        comparisons += [("combined", "brave"), ("staan", "brave")]

    print("\n=== Paired differences (A - B), bootstrap 95% CI, win/tie/loss by query ===")
    for a, b in comparisons:
        for name in ["gold_ndcg", "judge_ndcg"]:
            differences = (wide[name][a] - wide[name][b]).to_numpy()
            low, high = bootstrap_ci(differences)
            wins = int(np.sum(differences > 1e-9))
            losses = int(np.sum(differences < -1e-9))
            ties = len(differences) - wins - losses
            print(
                f"{a:>20} - {b:<20} {name:<10} {differences.mean():+.4f}  [{low:+.4f}, {high:+.4f}]"
                f"  W/T/L {wins}/{ties}/{losses}"
            )

    print("\n=== Mwmbl's results inside the combined top ten ===")
    for key, value in mwmbl_contribution(rows).items():
        print(f"{key}: {value}")


def measure_latency(dataset: pd.DataFrame, num_queries: int, include_brave: bool) -> None:
    """Raw provider latency, uncached, interleaved so both providers share the conditions.

    Staan is Combined Search's critical path: Wikipedia is fetched concurrently with it (and
    is cached and circuit-broken in production), and ranking against the local index is
    in-process. Wikipedia is not timed here because a burst of uncached calls gets rate-limited.
    """
    queries = sorted(sample_queries(dataset, 0.05))[:num_queries]
    timings = {"staan": []}
    if include_brave:
        timings["brave"] = []
    staan_headers = {"Authorization": f"Bearer {settings.STAAN_SEARCH_API_KEY}"}

    for query in queries:
        start = time.perf_counter()
        requests.get(
            settings.STAAN_SEARCH_URL,
            params={"q": query, "market": settings.STAAN_MARKET},
            headers=staan_headers,
            timeout=30,
        ).raise_for_status()
        timings["staan"].append(time.perf_counter() - start)

        if include_brave:
            start = time.perf_counter()
            _brave_request(query)
            timings["brave"].append(time.perf_counter() - start)
            time.sleep(BRAVE_MIN_INTERVAL_SECONDS)

    print(f"\n=== Uncached latency over {len(queries)} queries (seconds) ===")
    print(f"{'provider':<12} {'p50':>6} {'p90':>6} {'p99':>6} {'mean':>6}")
    for provider, values in timings.items():
        p50, p90, p99 = np.percentile(values, [50, 90, 99])
        print(f"{provider:<12} {p50:6.3f} {p90:6.3f} {p99:6.3f} {np.mean(values):6.3f}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "latency.json").write_text(json.dumps({k: [float(v) for v in vs] for k, vs in timings.items()}))


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--fraction", type=float, default=0.05, help="Fraction of gold test queries to sample.")
    parser.add_argument("--latency", type=int, default=0, help="Also time N uncached calls to each provider.")
    parser.add_argument("--latency-only", action="store_true", help="Skip the quality comparison.")
    args = parser.parse_args()

    include_brave = bool(BRAVE_SEARCH_API_KEY)
    if not include_brave:
        print("BRAVE_SEARCH_API_KEY is not set: skipping the Brave arm.")

    dataset = pd.read_csv(RANKINGS_DATASET_TEST_PATH)
    if args.latency_only:
        measure_latency(dataset, args.latency, include_brave)
        return

    judge = Judge(Path(settings.SUPER_SEARCH_JUDGE_MODEL_DIR))
    rows = collect(dataset, args.fraction, include_brave, judge)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"rows-{args.fraction}.json").write_text(json.dumps(rows))
    report(rows)

    if args.latency:
        measure_latency(dataset, args.latency, include_brave)


if __name__ == "__main__":
    run()
