"""Evaluate serving Wikipedia results from our own index instead of the API.

Today every search makes one live call to the Wikipedia search endpoint, cached by a
filesystem HTTP cache that grows without bound and writes the raw user query to disk. The
proposal under test: index the results a call returns against the query's unigrams and
bigrams, and on later queries skip the call when the index already returned enough
Wikipedia results. Goals are fewer calls, faster results, and deleting the disk cache. The
question this harness answers is what that costs in NDCG.

Two pieces make a realistic measurement possible:

``OverlayIndex``
    The local dev index (11 MB, NUM_PAGES=2560) returns <= 3 results for almost every
    query, so a gate evaluated against it would fire on nothing and mean nothing. The
    realistic index is the production one, reachable only read-only over HTTP. So reads
    come from ``RemoteIndex`` *unioned with* a fresh local ``TinyIndex`` that everything
    written during the run goes into. Both are duck-typed against ``retrieve()``, which is
    all ``Ranker`` uses.

``CountingWikiFetcher``
    Counts the calls the policy *would* make - the headline cost metric, and exact - while
    serving the response from a joblib cache, so Wikipedia is hit at most once per distinct
    query ever, reruns are free and reproducible, and we never trip the rate limiter that
    SUPER_SEARCH_EVAL_FINDINGS.md finding #6 records silently losing 36% of wiki results.
    We *count* the calls; we do not *make* them repeatedly.

Two questions, two arm sets (``--arm-set``):

``gates``
    The original one: which rule for skipping the API call preserves NDCG. Every firing
    is both "a call was skipped" and "stored documents were served", so the two effects
    are inseparable.

``scores``
    Wikipedia is called on *every* query and the results are stored, so nothing is ever
    skipped and the only thing that varies is what a stored document looks like to the
    ranker when a *different* query retrieves it. This is the "does storing help in
    general" question, and it is reported on the **affected subset**: the queries whose
    candidate pool held documents an earlier, different query stored. The overlay's local
    half contains only what the run stored and storing happens after retrieval, so on a
    gold set with no repeats a local-half hit *is* a cross-query hit - see
    ``OverlayIndex.retrieve``. That subset is hundreds of queries where the gates' fired
    subsets were 9 and 35, and it is compared paired, per query, against ``live-wiki``.

Three limits on what the numbers mean, none of them incidental:

  * **The gold set has no repeat queries** - 5,969 unique queries in the test split, each
    appearing once. Every hit here comes from term overlap between *different* queries,
    never from a repeat, so the measured call-avoidance rate is a **lower bound** on the
    production saving. ``--zipf`` replays the same queries as a repeat-weighted stream to
    bracket the real number (call counts only; no NDCG).
  * **Do not quote the wall clock.** With ``RemoteIndex``, ``get_results`` makes one HTTP
    call per term ∪ bigram ∪ completion to api.mwmbl.org, absorbed by a never-expiring
    filesystem cache, and the wiki fetch is joblib-cached. Derive the latency claim from
    the call-avoidance rate times a separately measured live Wikipedia call latency.
  * **Eviction is not measured.** The overlay starts empty, so nothing it stores displaces
    anything. Production pages are >90% full and ``_write_page`` silently truncates the
    tail of an oversized page, so there every stored document evicts about as much as it
    adds. Measure page occupancy on the production index before rollout.

Usage::

    DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \\
        uv run python -m mwmbl.rankeval.evaluation.evaluate_wiki_index --fraction 0.02
"""
import logging
import os
import shutil
import time
from argparse import ArgumentParser
from dataclasses import dataclass, field
from pathlib import Path

import django
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.test import override_settings  # noqa: E402
from joblib import Memory  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate import (  # noqa: E402
    NUM_RESULTS_FOR_EVAL, gold_scores_for, mean_sem, query_ndcg)
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel  # noqa: E402
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex  # noqa: E402
from mwmbl.rankeval.paths import (  # noqa: E402
    DATA_DIR, RANKINGS_DATASET_TEST_PATH, RANKINGS_DATASET_TRAIN_PATH, RUST_MODEL_PATH)
from mwmbl.tinysearchengine.indexer import (  # noqa: E402
    PAGE_SIZE, Document, DocumentState, TinyIndex)
from mwmbl.tinysearchengine.ltr import RustXGBPipeline  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402
from mwmbl.tinysearchengine.rank import get_wiki_results  # noqa: E402
from mwmbl.tinysearchengine.wiki_index_cache import (  # noqa: E402
    COUNTING_GATES, VALUE_GATES, store_wiki_results)
from mwmbl.tokenizer import tokenize  # noqa: E402


# Roughly 6k queries * ~3 storable terms each is well under 20k distinct terms, so 64k
# pages keeps collisions rare enough that the silent page-full truncation in _write_page
# does not confound the measurement. 256 MB on disk, recreated per arm.
DEFAULT_OVERLAY_PAGES = 65536

memory = Memory(location=str(DATA_DIR / "wiki-index-eval-cache"), verbose=0)


@memory.cache
def _fetch_wiki(query: str, num_results: int) -> list[dict]:
    """One real call to the Wikipedia search API, cached forever on disk.

    Cached as plain dicts rather than Documents so the cache survives changes to the
    Document class. Note the ``term``: get_wiki_results stamps each result with the *raw*
    query, and we keep that here deliberately, so the write path is exercised with the
    same input production would give it.
    """
    return [{"title": d.title, "url": d.url, "extract": d.extract, "score": d.score,
             "term": d.term}
            for d in get_wiki_results(query, num_results)]


class CountingWikiFetcher:
    """Drop-in for get_wiki_results that counts the calls a policy makes."""

    def __init__(self):
        self.calls = 0

    def __call__(self, query: str, num_results: int) -> list[Document]:
        self.calls += 1
        return [Document(d["title"], d["url"], d["extract"], d["score"], d["term"],
                         state=DocumentState.FROM_WIKI)
                for d in _fetch_wiki(query, num_results)]


class CountingWikiStore:
    """Drop-in for store_wiki_results that counts what actually got written.

    Also answers the WIKI_INDEX_REQUIRE_EXISTING_TERM question against the *overlay*.
    Left to itself store_wiki_results would ask the index it writes to, which here is
    only the local half and starts empty - so no term would ever exist and the strict
    arm would degenerate into storing nothing at all.
    """

    def __init__(self, index: 'OverlayIndex'):
        self.index = index
        self.urls_stored = 0

    def __call__(self, query: str, documents: list[Document], index_path) -> int:
        stored = store_wiki_results(query, documents, index_path,
                                    term_exists=self.index.has_term)
        self.urls_stored += stored
        return stored


class OverlayIndex:
    """Reads from a remote index unioned with a local writable one; writes go local.

    The remote production index is the only realistic thing to evaluate a gate against,
    and it is read-only over HTTP. Everything this run stores goes into a fresh local
    TinyIndex, and retrieve() returns both - which is what the production index would
    look like once it had been accumulating results.
    """

    def __init__(self, local_path: Path, num_pages: int, remote: RemoteIndex = None):
        self.local_path = local_path
        self.remote = remote if remote is not None else RemoteIndex()
        if local_path.exists():
            local_path.unlink()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        TinyIndex.create(item_factory=Document, index_path=str(local_path),
                         num_pages=num_pages, page_size=PAGE_SIZE)
        # Held open read-only for the arm's lifetime. Writers (index_pages) open their own
        # 'w' handle on the same file; both mmap MAP_SHARED, so writes are visible here
        # immediately - the same arrangement production relies on (see search_setup).
        self.local = TinyIndex(item_factory=Document, index_path=str(local_path))
        self.local.__enter__()
        # Set to a fresh set() before a query to collect the stored (term, url) pairs that
        # query retrieved. Everything in the local half was stored by an *earlier* query -
        # external_search, which does the storing, runs after retrieval, and the gold set
        # has no repeats - so a non-empty trace is exactly "this query saw another query's
        # stored documents", with no provenance bookkeeping needed.
        self.trace: set[tuple[str, str]] | None = None

    def retrieve(self, term: str) -> list[Document]:
        local = self.local.retrieve(term)
        if self.trace is not None:
            self.trace.update((document.term, document.url) for document in local)
        return self.remote.retrieve(term) + local

    def has_term(self, term: str) -> bool:
        """Whether the corpus - both halves - already holds documents under this term."""
        return bool(self.retrieve(term))

    def close(self):
        self.local.__exit__(None, None, None)
        self.local_path.unlink(missing_ok=True)


@dataclass
class Arm:
    """One policy to evaluate. Defaults are the proposal at its recommended settings."""
    name: str
    include_wiki: bool = True
    cache_enabled: bool = True
    gate: str = "ranked_top_n"
    threshold: int = 2
    require_existing_term: bool = False
    max_term_tokens: int = 2
    score_terms: str = "all"
    score_ema_alpha: float = 1.0


@dataclass
class ArmResult:
    arm: Arm
    ndcg: dict = field(default_factory=dict)
    proportion: dict = field(default_factory=dict)
    wiki_in_top: dict = field(default_factory=dict)
    called_wiki: dict = field(default_factory=dict)
    stored: dict = field(default_factory=dict)
    carried: dict = field(default_factory=dict)
    carried_in_top: dict = field(default_factory=dict)
    duration: dict = field(default_factory=dict)
    order: list = field(default_factory=list)

    @property
    def calls(self) -> int:
        return sum(self.called_wiki.values())

    @property
    def fired(self) -> list:
        """Queries where the gate suppressed a call it would otherwise have made."""
        return [q for q, called in self.called_wiki.items() if not called]

    @property
    def affected(self) -> list:
        """Queries whose candidate pool held documents an earlier, different query stored.

        The population this feature is supposed to help. Unlike `fired` it exists with the
        gate off, and it is a term-overlap event rather than a repeated query - so it is
        the subset that answers whether storing Wikipedia results makes the index better
        in general rather than only caching.
        """
        return [q for q, count in self.carried.items() if count]


# The per-term gates live on wildly different scales, so one shared sweep cannot serve
# them. The LTR model's predictions on live Wikipedia results run 0 to ~0.04 with a
# median near 0.003 (it filters at > 0.0 and the tail collapses toward zero), and
# mean_max divides by every query term including the ones with nothing, so it sits lower
# again. Coverage is a fraction, and the stored score is Wikipedia's rank, 3/2/1.
DEFAULT_VALUE_THRESHOLDS = {
    "mean_max_ltr": [0.0002, 0.0005, 0.001, 0.002, 0.005],
    "min_max_ltr": [0.0002, 0.0005, 0.001, 0.002, 0.005],
    "max_max_ltr": [0.001, 0.0025, 0.005, 0.01, 0.02],
    "term_coverage": [0.25, 0.5, 0.75, 1.0],
    "bigram_coverage": [0.5, 0.75, 1.0],
    "mean_max_stored": [0.75, 1.5, 2.25, 3.0],
}


def format_threshold(threshold: float) -> str:
    return f"{threshold:g}".replace(".", "_")


def build_arms(gates: list[str], thresholds: list[float],
               value_thresholds: list[float]) -> list[Arm]:
    """The standard arm set.

    Counting gates take a document count, the per-term profile gates take a score or a
    fraction, so the two families sweep over different threshold lists.
    """
    arms = [
        Arm("no-wiki", include_wiki=False),
        Arm("live-wiki", cache_enabled=False),
        Arm("indexed-nogate", gate="never"),
    ]
    for gate in gates:
        if gate not in VALUE_GATES:
            sweep = thresholds
        else:
            sweep = value_thresholds or DEFAULT_VALUE_THRESHOLDS[gate]
        arms += [Arm(f"indexed-{gate}-t{format_threshold(t)}", gate=gate, threshold=t)
                 for t in sweep]
    arms += [
        Arm("indexed-unigrams-only", max_term_tokens=1),
        Arm("indexed-noscore", gate="from_wiki_only", threshold=1, score_terms="none"),
        Arm("indexed-safe", require_existing_term=True),
        Arm("never-call", gate="always"),
    ]
    arms += build_coverage_arms()
    return arms


def build_coverage_arms() -> list[Arm]:
    """The 2x2 of coverage denominator against score policy.

    The sweep above runs every gate at the default score_terms="all", where the two
    denominators are provably the same condition (see query_bigrams) - so the gate on its
    own measures nothing and has to be paired with the score policy explicitly. Offline
    replay of a 3,000-position repeat-weighted stream over 2,347 cached queries gives the
    firing counts; what these arms add is what those firings cost in NDCG.

        denominator / scores      fired   repeats   first-seen
        terms       / all           690       672           18
        terms       / specific      227       227            0   <- gate dies
        bigrams     / all           690       672           18   <- identical to row 1
        bigrams     / specific      677       672            5   <- same savings, fewer
                                                                    wrong-query firings

    Threshold is pinned at 1.0. Anything lower fires when only some of a query's bigrams
    are covered, which is the partial-match case every gate in this harness has paid
    around -0.19 for; the sweep in DEFAULT_VALUE_THRESHOLDS is there to confirm that, not
    because a lower setting is a candidate.
    """
    return [
        Arm("indexed-bigram_coverage-all-t1", gate="bigram_coverage", threshold=1.0),
        Arm("indexed-bigram_coverage-specific-t1", gate="bigram_coverage", threshold=1.0,
            score_terms="specific"),
        Arm("indexed-term_coverage-specific-t1", gate="term_coverage", threshold=1.0,
            score_terms="specific"),
    ]


def build_score_arms() -> list[Arm]:
    """Arms that isolate the *ranking* effect of stored results from the caching effect.

    Every one runs gate="never": Wikipedia is called on every query and the results are
    stored, so no call is ever skipped and the only thing that varies is what a stored
    document looks like to the ranker when some *other* query retrieves it. That is the
    question the gate sweep could never answer, because a gate firing and a stored
    document being served were the same event.

    live-wiki stores nothing at all and is the baseline. The rest differ only in which
    term carries the Wikipedia rank, and whether re-storing overwrites or averages - so
    they all store the same (term, url) pairs and the affected subset is identical across
    them, which keeps the comparison paired.
    """
    return [
        Arm("live-wiki", cache_enabled=False),
        Arm("stored-score-none", gate="never", score_terms="none"),
        Arm("stored-score-all", gate="never", score_terms="all"),
        Arm("stored-score-specific", gate="never", score_terms="specific"),
        Arm("stored-score-exact", gate="never", score_terms="exact"),
        Arm("stored-score-specific-ema0_5", gate="never", score_terms="specific",
            score_ema_alpha=0.5),
        Arm("stored-score-specific-ema0_25", gate="never", score_terms="specific",
            score_ema_alpha=0.25),
    ]


def build_stack(arm: Arm, overlay: Path, overlay_pages: int, model):
    """The production stack (LTR + MMR) over a fresh overlay, instrumented for one arm."""
    fetcher = CountingWikiFetcher()
    index = OverlayIndex(overlay, overlay_pages)
    store = CountingWikiStore(index)
    ranker = MMRRanker(LTRRanker(
        index, DummyCompleter(), model,
        include_wiki=arm.include_wiki, num_wiki_results=3,
        wiki_fetcher=fetcher, wiki_store=store, wiki_index_path=str(index.local_path),
    ))
    return index, fetcher, store, MwmblRankingModel(ranker)


def arm_settings(arm: Arm):
    return override_settings(
        WIKI_INDEX_CACHE_ENABLED=arm.cache_enabled,
        WIKI_INDEX_GATE=arm.gate,
        WIKI_INDEX_GATE_THRESHOLD=arm.threshold,
        WIKI_INDEX_REQUIRE_EXISTING_TERM=arm.require_existing_term,
        WIKI_INDEX_MAX_TERM_TOKENS=arm.max_term_tokens,
        WIKI_INDEX_SCORE_TERMS=arm.score_terms,
        WIKI_INDEX_SCORE_EMA_ALPHA=arm.score_ema_alpha,
    )


def run_arm(arm: Arm, queries: list[str], gold: dict, overlay_pages: int,
            model, scratch: Path) -> ArmResult:
    index, fetcher, store, ranking_model = build_stack(
        arm, scratch / f"overlay-{arm.name}.tinysearch", overlay_pages, model)
    result = ArmResult(arm=arm, order=list(queries))
    try:
        with arm_settings(arm):
            for i, query in enumerate(queries):
                calls_before, stored_before = fetcher.calls, store.urls_stored
                index.trace = set()
                start = time.perf_counter()
                urls = ranking_model.predict(query)
                result.duration[query] = time.perf_counter() - start

                top = urls[:NUM_RESULTS_FOR_EVAL]
                carried_urls = {url for _, url in index.trace}
                result.ndcg[query] = query_ndcg(urls, gold[query])
                result.proportion[query] = len(set(top) & gold[query].keys()) / NUM_RESULTS_FOR_EVAL
                result.wiki_in_top[query] = sum(1 for url in top if "en.wikipedia.org" in url)
                result.called_wiki[query] = fetcher.calls > calls_before
                result.stored[query] = store.urls_stored - stored_before
                result.carried[query] = len(index.trace)
                result.carried_in_top[query] = sum(1 for url in top if url in carried_urls)

                if (i + 1) % 100 == 0:
                    print(f"  {arm.name}: {i + 1}/{len(queries)} queries, "
                          f"{fetcher.calls} wiki calls", flush=True)
    finally:
        index.close()
    return result


def run_zipf(arm: Arm, queries: list[str], gold: dict, stream_length: int,
             overlay_pages: int, model, scratch: Path, seed: int = 42) -> list[dict]:
    """Replay the queries as a repeat-weighted stream; one record per position.

    This closes the blind spot in the main table. The gold set has no repeats, so *every*
    gate firing measured there is a term-overlap firing: the index answered with Wikipedia
    pages that merely look related. A repeat is a completely different case - the stored
    documents are the very URLs the API returned last time - and production traffic is
    mostly repeats. Scoring each position and splitting on whether the query has been seen
    before separates the two, which is the difference between "caching" and "guessing".

    The Zipf weighting is arbitrary (rank-1 exponent over an arbitrary query order), so
    the absolute repeat rate is a modelling assumption, not a measurement. What the split
    shows is the *shape*: what a hit costs when it is a real repeat versus when it is not.
    """
    rng = np.random.default_rng(seed)
    weights = 1.0 / (np.arange(1, len(queries) + 1) ** 1.0)
    weights /= weights.sum()
    order = list(rng.choice(np.array(queries, dtype=object), stream_length,
                            replace=True, p=weights))

    index, fetcher, _, ranking_model = build_stack(
        arm, scratch / f"overlay-zipf-{arm.name}.tinysearch", overlay_pages, model)
    records = []
    seen: set[str] = set()
    try:
        with arm_settings(arm):
            for query in order:
                calls_before = fetcher.calls
                urls = ranking_model.predict(query)
                records.append({
                    "query": query,
                    "repeat": query in seen,
                    "called": fetcher.calls > calls_before,
                    "ndcg": query_ndcg(urls, gold[query]),
                })
                seen.add(query)
    finally:
        index.close()
    return records


def report_zipf(records_by_arm: dict, baseline_name: str = "live-wiki"):
    """Every arm replays the same stream from the same seed, so position i is the same
    query in all of them and the Δ can be paired the way report_affected's is."""
    baseline = records_by_arm.get(baseline_name)

    def mean_over(records, predicate):
        return [r["ndcg"] for r in records if predicate(r)]

    def paired_over(records, predicate):
        return [r["ndcg"] - b["ndcg"] for r, b in zip(records, baseline) if predicate(r)]

    print(f"\n{'arm':<36} {'calls':>13} {'NDCG on repeats':>20} "
          f"{'NDCG first-seen':>20} {'Δ paired on repeats':>22}")
    print("-" * 110)
    for name, records in records_by_arm.items():
        calls = sum(1 for r in records if r["called"])
        repeats = mean_over(records, lambda r: r["repeat"])
        first = mean_over(records, lambda r: not r["repeat"])
        if baseline is None or name == baseline_name:
            delta = "n/a"
        else:
            delta = mean_sem(paired_over(records, lambda r: r["repeat"]))
        print(f"{name:<36} {calls:>5}/{len(records):<7} {mean_sem(repeats):>20} "
              f"{mean_sem(first):>20} {delta:>22}")


def paired_deltas(result: ArmResult, baseline: ArmResult, queries: list) -> list[float]:
    """Per-query NDCG differences against the baseline on the same queries.

    Paired, unlike the difference of two independent means the main table prints: both
    arms answered the identical query against the identical remote index, so the query's
    own difficulty cancels and the error bar shrinks to the variance of the *effect*.
    That matters here because the interesting subsets are small.
    """
    return [result.ndcg[query] - baseline.ndcg[query] for query in queries]


def report_affected(results: list[ArmResult], reference_name: str,
                    baseline_name: str = "live-wiki"):
    """What happens to a query that retrieves a document some other query stored.

    The subset is taken from one reference arm and applied to all of them: the score arms
    store the same (term, url) pairs and differ only in the score written, so they share a
    subset, and using one keeps every arm scored on identical queries. The baseline stores
    nothing, so it has no subset of its own - it is what the subset is measured against.
    """
    baseline = next((r for r in results if r.arm.name == baseline_name), None)
    reference = next((r for r in results if r.arm.name == reference_name), None)
    if baseline is None or reference is None:
        return

    affected = [q for q in reference.order if reference.carried[q]]
    if not affected:
        print(f"\nNo query retrieved another query's stored documents in {reference_name}.")
        return

    short = [q for q in affected if len(tokenize(q)) == 1]
    longer = [q for q in affected if len(tokenize(q)) > 1]
    print(f"\nQueries that retrieved documents an earlier, different query stored "
          f"({len(affected)}/{len(reference.order)}, from {reference_name}):")
    print(f"  {len(short)} one-token, {len(longer)} multi-token. Paired NDCG against "
          f"{baseline_name} on the same queries.")
    print(f"\n{'arm':<36} {'Δ paired (affected)':>24} {'Δ 1-token':>24} "
          f"{'Δ 2+ token':>24} {'carried@10':>11}")
    print("-" * 118)
    for result in results:
        if result.arm.name == baseline_name:
            continue
        carried_top = np.mean([result.carried_in_top[q] for q in affected])
        print(f"{result.arm.name:<36} "
              f"{mean_sem(paired_deltas(result, baseline, affected)):>24} "
              f"{mean_sem(paired_deltas(result, baseline, short)):>24} "
              f"{mean_sem(paired_deltas(result, baseline, longer)):>24} "
              f"{carried_top:>11.2f}")

    print(f"\nPaired Δ over all {len(reference.order)} queries, for comparison:")
    for result in results:
        if result.arm.name == baseline_name:
            continue
        print(f"  {result.arm.name:<36} "
              f"{mean_sem(paired_deltas(result, baseline, reference.order)):>24}")


def _second_half(result: ArmResult, values: dict) -> list:
    """Steady-state slice: the tail of the stream, after the index has warmed up."""
    tail = result.order[len(result.order) // 2:]
    return [values[q] for q in tail]


def report(results: list[ArmResult], header: str = "",
           baseline_name: str = "live-wiki"):
    baseline = next((r for r in results if r.arm.name == baseline_name), None)
    n = len(results[0].order)
    if header:
        print(f"\n{header}")

    print(f"\n{'=' * 132}")
    print(f"{'arm':<36} {'NDCG@10':>18} {'ΔNDCG':>9} {'proportion':>18} "
          f"{'wiki@10':>7} {'calls':>12} {'stored':>8} {'affected':>9} {'carried@10':>11}")
    print(f"{'-' * 132}")
    for result in results:
        ndcg = list(result.ndcg.values())
        delta = (f"{np.mean(ndcg) - np.mean(list(baseline.ndcg.values())):+.4f}"
                 if baseline else "n/a")
        print(f"{result.arm.name:<36} {mean_sem(ndcg):>18} {delta:>9} "
              f"{mean_sem(list(result.proportion.values())):>18} "
              f"{np.mean(list(result.wiki_in_top.values())):>7.2f} "
              f"{result.calls:>5}/{n:<6} {sum(result.stored.values()):>8} "
              f"{len(result.affected):>9} "
              f"{np.mean(list(result.carried_in_top.values())):>11.2f}")
    print(f"{'=' * 132}")

    print("\nSteady state (second half of the stream, index already warm):")
    print(f"{'arm':<36} {'NDCG@10':>18} {'calls avoided':>15}")
    for result in results:
        tail = result.order[len(result.order) // 2:]
        avoided = sum(1 for q in tail if not result.called_wiki[q])
        print(f"{result.arm.name:<36} {mean_sem(_second_half(result, result.ndcg)):>18} "
              f"{avoided:>6}/{len(tail):<8}")

    if baseline is None:
        return
    print("\nOn the queries where the gate fired - the subset the feature is decided on:")
    print(f"{'arm':<36} {'fired':>13} {'NDCG (arm)':>18} {'NDCG (live-wiki)':>18} {'Δ':>9}")
    for result in results:
        fired = result.fired
        if not fired or result.arm.name == baseline_name or not result.arm.include_wiki:
            continue
        arm_ndcg = [result.ndcg[q] for q in fired]
        base_ndcg = [baseline.ndcg[q] for q in fired]
        print(f"{result.arm.name:<36} {len(fired):>5}/{n:<7} {mean_sem(arm_ndcg):>18} "
              f"{mean_sem(base_ndcg):>18} {np.mean(arm_ndcg) - np.mean(base_ndcg):>+9.4f}")

    stored_nothing = [r for r in results if r.arm.cache_enabled and r.arm.include_wiki]
    if stored_nothing:
        print("\nFetches that stored nothing (Wikipedia's results did not contain the query's")
        print("words, so nothing could be filed - these queries can never become hits):")
        for result in stored_nothing:
            fetched = [q for q, called in result.called_wiki.items() if called]
            empty = [q for q in fetched if result.stored[q] == 0]
            if fetched:
                print(f"  {result.arm.name:<36} {len(empty):>5}/{len(fetched):<6} "
                      f"({100 * len(empty) / len(fetched):.1f}% of fetches)")

    print("\nNOTE: wall-clock timings from this harness are not production latency - the "
          "remote\nindex makes one HTTP call per query term and the wiki fetch is disk-cached. "
          "Mean\nper-query predict time, for reference only:")
    for result in results:
        print(f"  {result.arm.name:<36} {np.mean(list(result.duration.values())):.3f}s")


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--fraction", type=float, default=0.05,
                        help="Fraction of gold queries to sample.")
    parser.add_argument("--train", action="store_true",
                        help="Evaluate on the train split instead of test.")
    parser.add_argument("--arms", nargs="+", default=None,
                        help="Only run these arms by name (default: the whole set).")
    parser.add_argument("--arm-set", choices=["gates", "scores"], default="gates",
                        help="gates: sweep the skip-the-call rules. scores: call Wikipedia "
                             "every time and vary only which term carries a stored "
                             "result's score, to measure the ranking effect on other "
                             "queries in isolation.")
    # bigram_coverage is left out of the default sweep because build_coverage_arms
    # already runs it at the only threshold worth running, paired with both score
    # policies. Pass it explicitly to sweep its thresholds.
    parser.add_argument("--gates", nargs="+",
                        default=[g for g in list(COUNTING_GATES) + list(VALUE_GATES)
                                 if g != "bigram_coverage"],
                        help="Gate definitions to sweep.")
    parser.add_argument("--thresholds", type=float, nargs="+", default=[1, 2, 3],
                        help="Counting gates: wiki results needed to skip the call.")
    parser.add_argument("--value-thresholds", type=float, nargs="+", default=None,
                        help="Per-term profile gates: score or fraction needed. "
                             "Defaults to a per-gate sweep, since they are on very "
                             "different scales.")
    parser.add_argument("--order", choices=["shuffled", "alpha"], default="shuffled",
                        help="Query order. Alphabetical clusters shared-prefix queries "
                             "adjacently and inflates the hit rate; shuffled is the "
                             "headline number.")
    parser.add_argument("--overlay-pages", type=int, default=DEFAULT_OVERLAY_PAGES,
                        help="Pages in the per-arm overlay index.")
    parser.add_argument("--zipf", type=int, default=0,
                        help="Also replay a repeat-weighted stream of this many queries "
                             "and report call counts only (no NDCG).")
    # Per-process by default: the run deletes its scratch directory when it finishes,
    # so a second run sharing one would pull the overlay out from under the first.
    parser.add_argument("--scratch", default=os.environ.get(
        "WIKI_INDEX_EVAL_SCRATCH", f"/tmp/mwmbl-wiki-index-eval-{os.getpid()}"),
        help="Where the per-arm overlay indexes are created.")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the cached Wikipedia responses before evaluating.")
    parser.add_argument("--verbose", action="store_true",
                        help="Keep the per-query INFO logging from the ranker and indexer.")
    args = parser.parse_args()

    if not args.verbose:
        # One line per retrieval and per stored page drowns the results table.
        logging.getLogger("mwmbl").setLevel(logging.WARNING)

    if args.clear_cache:
        memory.clear(warn=False)

    path = RANKINGS_DATASET_TRAIN_PATH if args.train else RANKINGS_DATASET_TEST_PATH
    print("Evaluating against dataset", path)
    dataset = pd.read_csv(path)

    all_queries = dataset["query"].unique()
    rng = np.random.default_rng(42)  # same seed/sampling as evaluate.evaluate
    if args.fraction < 1.0:
        sampled = set(rng.choice(all_queries, int(args.fraction * len(all_queries)),
                                 replace=False))
    else:
        sampled = set(all_queries)

    gold = {query: gold_scores_for(rankings)
            for query, rankings in dataset.groupby("query") if query in sampled}
    queries = sorted(gold)
    if args.order == "shuffled":
        np.random.default_rng(42).shuffle(queries)
    print(f"Num queries {len(queries)} ({args.order} order)")

    if args.arm_set == "scores":
        arms = build_score_arms()
    else:
        arms = build_arms(args.gates, args.thresholds, args.value_thresholds)
    if args.arms:
        by_name = {arm.name: arm for arm in arms}
        unknown = [name for name in args.arms if name not in by_name]
        if unknown:
            raise SystemExit(f"Unknown arm(s) {unknown}. Available: {sorted(by_name)}")
        arms = [by_name[name] for name in args.arms]

    model = RustXGBPipeline.from_model_path(str(RUST_MODEL_PATH))
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    results = []
    try:
        for arm in arms:
            print(f"\n--- {arm.name} (gate={arm.gate} threshold={arm.threshold} "
                  f"cache={arm.cache_enabled} wiki={arm.include_wiki}) ---", flush=True)
            results.append(run_arm(arm, queries, gold, args.overlay_pages, model, scratch))

        report(results, header=f"{len(queries)} queries, {args.order} order, "
                              f"{'train' if args.train else 'test'} split, "
                              f"fraction={args.fraction}, overlay_pages={args.overlay_pages}")

        reference = next((r.arm.name for r in results
                          if r.arm.cache_enabled and r.arm.include_wiki), None)
        if reference is not None:
            report_affected(results, reference_name=reference)

        if args.zipf:
            print(f"\nRepeat-weighted stream: {args.zipf} positions, Zipf over the same "
                  f"{len(queries)} queries.\nThe gold set has no repeats, so the table above "
                  "measures only term-overlap hits.\nHere a hit can also be a genuine repeat, "
                  "where the index holds the very URLs the\nAPI returned - which is what "
                  "production traffic mostly consists of.")
            records_by_arm = {}
            for arm in arms:
                if not arm.include_wiki:
                    continue
                records_by_arm[arm.name] = run_zipf(
                    arm, queries, gold, args.zipf, args.overlay_pages, model, scratch)
            report_zipf(records_by_arm)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    run()
