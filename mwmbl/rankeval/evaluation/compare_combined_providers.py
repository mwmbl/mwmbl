"""Compare Combined Search with Staan alone and with Brave, on the production index.

Two questions, one run:

1. Does Combined Search beat Staan on its own - i.e. do Mwmbl's results earn their place?
2. How does Combined Search compare with Brave, on quality and on latency?

The arms, each scored on the same sampled gold test queries:

- ``staan``: Staan's top ten in Staan's own order - what a client gets calling Staan direct.
- ``combined``: the endpoint as shipped - production index + Staan, ``CombinedLTRRanker``
  with ``model-combined.xgb`` (Staan's rank as features, Staan's results exempt from the
  majority-terms filter), MMR.
- ``previous``: the endpoint before that - index + Staan + Wikipedia under the 50-feature
  combined model, with the filter applied to everything. Needs the old model at
  ``devdata/rankeval-2026-04/model-combined.xgb``.
- ``reference``: the Python XGBoost booster the shipped model was ported from
  (``model-combined-provider.json``, the ``provider-nowiki-keep`` arm in
  ``mwmbl/rankeval/combined-search-handover.md``), over the same pool as ``combined``.
  ``combined`` should land on it: the Rust and Python trainings are not bit-identical.
- ``brave``: the Brave Search API's top ten, when ``BRAVE_SEARCH_API_KEY`` is set.

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
from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH  # noqa: E402
from mwmbl.search_setup import combined_ltr_model  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.ltr import RustXGBPipeline  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker, LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402
from mwmbl.tinysearchengine.rank import get_wiki_results  # noqa: E402
from mwmbl.tinysearchengine.staan import get_staan_results  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text  # noqa: E402
from mwmbl_rank import FEATURE_NAMES  # noqa: E402
from mwmbl_rank import RustXGBPipeline as RustPipeline  # noqa: E402

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
# The free Brave plan allows one request per second.
BRAVE_MIN_INTERVAL_SECONDS = 1.1

OUTPUT_DIR = Path("devdata/combined_providers_eval")
PREVIOUS_MODEL_PATH = Path("devdata/rankeval-2026-04/model-combined.xgb")
REFERENCE_MODEL_PATH = Path("devdata/rankeval-2026-04/model-combined-provider.json")
NUM_BOOTSTRAP = 10_000
NUM_TERMS_INDEX = FEATURE_NAMES.index("num_terms")
MATCH_TERMS_INDEX = FEATURE_NAMES.index("match_terms")

memory = Memory(location="devdata/cache")


class ReferenceRanker(CombinedLTRRanker):
    """CombinedLTRRanker scored by the Python booster the shipped model was ported from."""

    def __init__(self, tiny_index, booster: xgb.Booster):
        super().__init__(tiny_index, DummyCompleter(), model=None)
        self.booster = booster

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        records = self.records(" ".join(terms), results)
        features = np.array(RustPipeline.extract_features(records, True), dtype=np.float32)
        predictions = self.booster.predict(xgb.DMatrix(features))
        fails_term_filter = features[:, MATCH_TERMS_INDEX] <= features[:, NUM_TERMS_INDEX] / 2.0
        from_staan = np.array([record["from_staan"] for record in records])
        kept = np.flatnonzero(~(fails_term_filter & ~from_staan))
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
    combined_ranker = MMRRanker(CombinedLTRRanker(remote_index, DummyCompleter(), combined_ltr_model))
    previous_model = RustXGBPipeline.from_model_path(str(PREVIOUS_MODEL_PATH))
    previous_ranker = MMRRanker(LTRRanker(remote_index, DummyCompleter(), previous_model, include_wiki=False))
    reference_ranker = MMRRanker(ReferenceRanker(remote_index, xgb.Booster(model_file=str(REFERENCE_MODEL_PATH))))

    def staan(query, staan_docs, wiki_docs):
        return _as_dicts(staan_docs)

    def combined(query, staan_docs, wiki_docs):
        return _as_dicts(combined_ranker.search(query, staan_docs, False))

    def previous(query, staan_docs, wiki_docs):
        return _as_dicts(previous_ranker.search(query, staan_docs + wiki_docs, False))

    def reference(query, staan_docs, wiki_docs):
        return _as_dicts(reference_ranker.search(query, staan_docs, False))

    def brave(query, staan_docs, wiki_docs):
        return brave_results(query)

    arms = {"staan": staan, "combined": combined, "previous": previous, "reference": reference}
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

    A combined result is Mwmbl's when Staan did not return it. The displaced results are
    Staan's top ten that did not make the combined top ten.
    """
    mwmbl_scores, displaced_scores, counts = [], [], []
    for row in rows:
        staan_urls = set(row["staan_urls"])
        combined = row["lists"]["combined"]
        mwmbl_in_combined = [url for url in combined if url not in staan_urls]
        displaced = [url for url in row["lists"]["staan"] if url not in combined]
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
    comparisons = [("combined", "staan"), ("combined", "previous"), ("combined", "reference"), ("reference", "staan")]
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

    Staan is Combined Search's critical path: it is the only network call, and ranking
    against the local index is in-process.
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
