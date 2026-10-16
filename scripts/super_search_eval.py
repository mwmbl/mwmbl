#!/usr/bin/env python3
"""Offline evaluation harness for Super Search source selection.

Subcommands:

  build-matrix  - query every source for each query in a query file and write a
                  dense (queries x sources) reward + feature matrix. Rewards are
                  either LTR-top-K survival (--reward survival, default) or the
                  fine-tuned relevance judge's mean per-source doc score
                  (--reward judge). The network fetch is checkpointed per query
                  (JSONL next to --out) and resumable. Requires network + Django.

  build-gold-matrix
                - build the same kind of matrix offline from the LTR dataset's
                  gold labels (no network): a (query x source) cell is "available"
                  (mask) if that source's domain appears in the query's LTR rows,
                  and gets reward 1.0 if any of those rows is gold-relevant. Lets
                  us learn/evaluate source selection against real gold relevance
                  instead of the LTR-top-K-survival proxy.

  select        - fit the XGBoost reward model on the matrix (grouped CV by
                  query) and print each feature's ablation drop in coverage@k.

  simulate      - replay Thompson sampling over the matrix, sweep the
                  exploration scale nu, and print it against random / popularity
                  / cosine / oracle baselines.

  simulate-xgb  - replay the epsilon-greedy XGBoost contextual bandit over the
                  matrix for each candidate epsilon, printed against the same
                  baselines and the best LinTS nu for a direct comparison.

  holdout       - standard offline evaluation of the xgb source model: split
                  queries train/test (stratified by home source), fit on train,
                  report test coverage@k and home-source recall@k vs baselines.
                  --queries-by-source supplies the {source: [queries]} home map.

Usage:
  DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python scripts/super_search_eval.py \
      build-matrix --queries queries.txt --out eval_matrix
  uv run python scripts/super_search_eval.py select   --matrix eval_matrix
  uv run python scripts/super_search_eval.py simulate --matrix eval_matrix
"""

import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import django
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")

# host_of/registrable are pure stdlib helpers (no Django) — safe to import at module
# load, before django.setup(); the registry-backed source_domain_map is loaded lazily.
from mwmbl.tinysearchengine.super_search_select.domains import (  # noqa: E402
    host_of,
    registrable,
)


def _bootstrap_django():
    django.setup()


# ---------------------------------------------------------------------------
# build-matrix (network + Django)
# ---------------------------------------------------------------------------


async def _query_all_sources(query: str, limit: int):
    import httpx
    from django.conf import settings

    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    timeout = settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": "mwmbl-super-search-eval/0.1"}
    ) as client:

        async def one(name, fn):
            try:
                docs = await asyncio.wait_for(fn(client, query, limit), timeout=timeout)
                return name, docs
            except Exception:
                return name, []

        results = await asyncio.gather(*[one(n, f) for n, f in SOURCES.items()])
    return dict(results)


def _fetch_all_queries(queries: list[str], checkpoint: Path, limit: int) -> dict[str, dict]:
    """Pass 1 with a resumable per-query JSONL checkpoint.

    Returns ``{query: {source: [[url, title, extract], ...]}}``. Queries already
    in the checkpoint are not re-fetched, so a multi-hour network run can be
    interrupted and resumed.
    """

    fetched: dict[str, dict] = {}
    if checkpoint.exists():
        for line in checkpoint.read_text().splitlines():
            rec = json.loads(line)
            fetched[rec["query"]] = rec["docs"]
        print(f"checkpoint {checkpoint}: {len(fetched)} queries already fetched")
    todo = [q for q in queries if q not in fetched]
    with checkpoint.open("a") as fh:
        for qi, query in enumerate(todo):
            docs_by_source = asyncio.run(_query_all_sources(query, limit))
            docs = {name: [[d.url, d.title, d.extract] for d in ds] for name, ds in docs_by_source.items() if ds}
            fetched[query] = docs
            fh.write(json.dumps({"query": query, "docs": docs}) + "\n")
            fh.flush()
            print(f"  [{qi + 1}/{len(todo)}] {query!r}: {sum(len(d) for d in docs.values())} docs")
    return fetched


def _survival_rewards(query: str, docs_by_source: dict, s_index: dict, R, qi: int, limit: int, top_k: int) -> None:
    """Reward = fraction of a source's results surviving the LTR model's top-K."""
    from mwmbl.search_setup import ltr_model
    from mwmbl.tinysearchengine.indexer import Document
    from mwmbl.tinysearchengine.ltr_rank import score_documents

    source_by_url, all_docs = {}, []
    for name, docs in docs_by_source.items():
        for url, title, extract in docs:
            if url and title:
                source_by_url.setdefault(url, name)
                all_docs.append(Document(title=title, url=url, extract=extract or ""))
    if not all_docs:
        return
    scores = score_documents(ltr_model, query, all_docs)
    ranked = [d.url for d, _ in sorted(zip(all_docs, scores), key=lambda x: -x[1])][:top_k]
    counts = {}
    for url in ranked:
        src = source_by_url.get(url)
        if src:
            counts[src] = counts.get(src, 0) + 1
    for name, c in counts.items():
        R[qi, s_index[name]] = min(c / max(limit, 1), 1.0)


def _judge_rewards(query: str, docs_by_source: dict, s_index: dict, R, qi: int) -> None:
    """Reward = mean relevance-judge score of the source's returned docs — the
    offline analog of rewards.compute_judge_rewards."""
    from mwmbl.tinysearchengine.super_search_select.judge import doc_text, get_judge

    judge = get_judge()
    if judge is None:
        raise RuntimeError(
            "--reward judge requires the relevance judge artifact (settings.SUPER_SEARCH_JUDGE_MODEL_DIR)"
        )
    for name, docs in docs_by_source.items():
        texts = [doc_text(title, extract) for _, title, extract in docs]
        if texts:
            R[qi, s_index[name]] = float(np.mean(judge.score(query, texts)))


def build_matrix(queries: list[str], out: str, reward: str = "survival", checkpoint: str | None = None):
    from django.conf import settings

    from mwmbl.tinysearchengine.super_search_select import vectors
    from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix
    from mwmbl.tinysearchengine.super_search_select.features import (
        FEATURE_NAMES,
        QueryContext,
        feature_vector,
    )
    from mwmbl.tinysearchengine.super_search_select.registry import get_meta
    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    sources = list(SOURCES.keys())
    s_index = {name: i for i, name in enumerate(sources)}
    limit = settings.SUPER_SEARCH_RESULTS_PER_SOURCE
    top_k = settings.SUPER_SEARCH_TOP_K
    dim = settings.SUPER_SEARCH_PROJECTION_DIM
    F = len(FEATURE_NAMES)

    # Pass 1: query everything (resumable), then accumulate each site's content
    # profile (mean of its projected result samples).
    cp = Path(checkpoint) if checkpoint else Path(f"{out}.fetch.jsonl")
    fetched = _fetch_all_queries(queries, cp, limit)
    prof_bow = {n: np.zeros(dim) for n in sources}
    prof_cng = {n: np.zeros(dim) for n in sources}
    for query in queries:
        for name, docs in fetched[query].items():
            text = " ".join(f"{title or ''} {extract or ''}" for _, title, extract in docs)
            prof_bow[name] += vectors.project_bow(text, dim)
            prof_cng[name] += vectors.project_char_ngrams(text, dim)
    profile = {n: (vectors._l2_normalise(prof_bow[n]), vectors._l2_normalise(prof_cng[n])) for n in sources}

    # Pass 2: features + rewards.
    Q, S = len(queries), len(sources)
    X = np.zeros((Q, S, F))
    R = np.zeros((Q, S))
    mask = np.zeros((Q, S), dtype=bool)
    for qi, query in enumerate(queries):
        docs_by_source = fetched[query]
        bow = vectors.project_bow(query, dim)
        cng = vectors.project_char_ngrams(query, dim)
        qctx = QueryContext.build(query, bow, cng)
        for name in sources:
            X[qi, s_index[name]] = feature_vector(qctx, get_meta(name), profile[name])
        for name in docs_by_source:
            mask[qi, s_index[name]] = True
        if reward == "judge":
            _judge_rewards(query, docs_by_source, s_index, R, qi)
        else:
            _survival_rewards(query, docs_by_source, s_index, R, qi, limit, top_k)
        if (qi + 1) % 50 == 0:
            print(f"  scored [{qi + 1}/{Q}]")

    matrix = RewardMatrix(queries=queries, sources=sources, feature_names=list(FEATURE_NAMES), X=X, R=R, mask=mask)
    matrix.save(out)
    print(
        f"Wrote matrix {out}.npz/.json ({reward} rewards): {Q} queries x {S} sources, {int(mask.sum())} filled cells."
    )


# ---------------------------------------------------------------------------
# build-gold-matrix (offline: LTR dataset gold labels, no network)
# ---------------------------------------------------------------------------


def _is_gold(rank) -> bool:
    """True if ``rank`` is a real gold rank (not pandas NaN / None / blank)."""
    if rank is None:
        return False
    if isinstance(rank, float) and math.isnan(rank):
        return False
    if isinstance(rank, str) and not rank.strip():
        return False
    return True


def attribute_rows(rows, reg_map):
    """Attribute LTR rows to Super Search sources by registrable domain (pure).

    ``rows`` is an iterable of ``(query, url, title, extract, gold_standard_rank)``
    and ``reg_map`` maps a registrable domain to the source names on it. Returns
    ``(per_query, prof_text)`` where ``per_query[query][source]`` is True iff that
    source has a gold-relevant row for the query (else False = available-but-not-gold),
    and ``prof_text[source]`` accumulates that source's title/extract text.
    """
    per_query: dict[str, dict[str, bool]] = {}  # query -> {source -> has_gold}
    prof_text: dict[str, list[str]] = defaultdict(list)
    for query, url, title, extract, gold in rows:
        names = reg_map.get(registrable(host_of(str(url))))
        if not names:
            continue
        is_gold = _is_gold(gold)
        q_sources = per_query.setdefault(str(query), {})
        text = f"{title or ''} {extract or ''}"
        for name in names:
            q_sources[name] = q_sources.get(name, False) or is_gold
            prof_text[name].append(text)
    return per_query, prof_text


def build_gold_matrix(out: str):
    """Build a (query x source) reward matrix from the LTR dataset's gold labels.

    Unlike ``build-matrix`` (which queries live sources and rewards a source by how
    many of its results survive the LTR model's *own* top-K), this is fully offline
    and grounds the reward in real gold relevance:

      * mask[q, s] = source ``s``'s registrable domain appears in query ``q``'s LTR
        rows (the offline "this source contributed a candidate" signal);
      * R[q, s]    = 1.0 if any such row is gold-relevant (non-null
        ``gold_standard_rank``), else 0.0 (binary has-gold).

    Source content profiles for the cosine features are accumulated from the LTR
    rows' title/extract text per source, so no network access is needed.
    """
    import pandas as pd
    from django.conf import settings

    from mwmbl.rankeval.paths import LEARNING_TO_RANK_DATASET_PATH
    from mwmbl.tinysearchengine.super_search_select import vectors
    from mwmbl.tinysearchengine.super_search_select.domains import source_domain_map
    from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix
    from mwmbl.tinysearchengine.super_search_select.features import (
        FEATURE_NAMES,
        QueryContext,
        feature_vector,
    )
    from mwmbl.tinysearchengine.super_search_select.registry import get_meta, get_registry

    dim = settings.SUPER_SEARCH_PROJECTION_DIM
    F = len(FEATURE_NAMES)

    sources = list(get_registry().keys())
    s_index = {name: i for i, name in enumerate(sources)}
    reg_map = source_domain_map()  # registrable domain -> [source names]

    df = pd.read_csv(LEARNING_TO_RANK_DATASET_PATH, lineterminator="\n")
    print(f"Loaded LTR dataset: {len(df)} rows, {df['query'].nunique()} queries")

    # Pass 1: attribute every row to its source(s), recording per-(query, source)
    # availability + gold and accumulating per-source content text for the profiles.
    per_query, prof_text = attribute_rows(
        zip(df["query"], df["url"], df["title"], df["extract"], df["gold_standard_rank"]),
        reg_map,
    )

    # In-coverage queries only: those with >=1 in-source candidate row.
    in_cov_queries = list(per_query.keys())
    profile = {
        n: (
            vectors.project_bow(" ".join(prof_text[n]), dim) if prof_text[n] else None,
            vectors.project_char_ngrams(" ".join(prof_text[n]), dim) if prof_text[n] else None,
        )
        for n in sources
    }

    # Pass 2: features + binary has-gold reward.
    Q, S = len(in_cov_queries), len(sources)
    X = np.zeros((Q, S, F))
    R = np.zeros((Q, S))
    mask = np.zeros((Q, S), dtype=bool)
    for qi, query in enumerate(in_cov_queries):
        bow = vectors.project_bow(query, dim)
        cng = vectors.project_char_ngrams(query, dim)
        qctx = QueryContext.build(query, bow, cng)
        for name, has_gold in per_query[query].items():
            si = s_index[name]
            X[qi, si] = feature_vector(qctx, get_meta(name), profile[name])
            mask[qi, si] = True
            if has_gold:
                R[qi, si] = 1.0

    matrix = RewardMatrix(
        queries=in_cov_queries, sources=sources, feature_names=list(FEATURE_NAMES), X=X, R=R, mask=mask
    )
    matrix.save(out)

    # Diagnostics: does selection even have room to matter? If most queries have
    # few in-source candidates, "query them all" is as good as any learned policy.
    avail_per_q = mask.sum(axis=1)
    gold_per_q = (R > 0).sum(axis=1)
    n_gold_q = int((gold_per_q > 0).sum())
    print(f"\nWrote matrix {out}.npz/.json: {Q} in-coverage queries x {S} sources, {int(mask.sum())} filled cells.")
    print(f"  queries with >=1 gold source:   {n_gold_q} ({100 * n_gold_q / max(Q, 1):.1f}%)")
    print(
        f"  mean available sources / query: {avail_per_q.mean():.2f} "
        f"(median {int(np.median(avail_per_q))}, max {int(avail_per_q.max())})"
    )
    for k in (1, 2, 3, 5, 10):
        n = int((avail_per_q > k).sum())
        print(
            f"  queries with > {k:>2} available sources: {n:>4} "
            f"({100 * n / max(Q, 1):.1f}%)  <- room for top-{k} selection to matter"
        )


# ---------------------------------------------------------------------------
# select / simulate (pure, no network)
# ---------------------------------------------------------------------------


def cmd_select(matrix_path: str, k: int):
    from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix, select_features

    m = RewardMatrix.load(matrix_path)
    result = select_features(m, k=k)
    print(f"baseline coverage@{k}: {result['baseline_coverage']:.4f}\n")
    print("feature ablation (drop in coverage@k when removed; higher = more useful):")
    for name, drop in sorted(result["ablation_drop"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:18} {drop:+.4f}")


def cmd_simulate(matrix_path: str, k: int):
    from mwmbl.tinysearchengine.super_search_select.evaluation import (
        RewardMatrix,
        simulate_baselines,
        sweep_explore_scale,
    )

    m = RewardMatrix.load(matrix_path)
    base = simulate_baselines(m, k=k)
    print("baselines (mean captured reward per query):")
    for name, val in sorted(base.items(), key=lambda kv: -kv[1]):
        print(f"  {name:12} {val:.4f}")
    print("\nThompson sampling by exploration scale nu:")
    sweep = sweep_explore_scale(m, k=k, nus=[0.0, 0.25, 0.5, 1.0, 2.0, 4.0])
    best = max(sweep, key=sweep.get)
    for nu, val in sweep.items():
        marker = "  <- best" if nu == best else ""
        print(f"  nu={nu:<4} {val:.4f}{marker}")


def cmd_simulate_xgb(matrix_path: str, k: int, epsilons: list[float], refit_every: int, min_rows: int):
    from mwmbl.tinysearchengine.super_search_select.evaluation import (
        RewardMatrix,
        simulate_baselines,
        sweep_epsilon,
        sweep_explore_scale,
    )

    m = RewardMatrix.load(matrix_path)
    base = simulate_baselines(m, k=k)
    print("baselines (mean captured reward per query):")
    for name, val in sorted(base.items(), key=lambda kv: -kv[1]):
        print(f"  {name:12} {val:.4f}")
    ts = sweep_explore_scale(m, k=k, nus=[0.05, 0.25, 1.0])
    best_nu = max(ts, key=ts.get)
    print(f"\nbest LinTS: nu={best_nu} {ts[best_nu]:.4f}")
    print("\nXGB contextual bandit by epsilon:")
    sweep = sweep_epsilon(m, k=k, epsilons=epsilons, refit_every=refit_every, min_rows=min_rows)
    best = max(sweep, key=sweep.get)
    for eps, val in sweep.items():
        marker = "  <- best" if eps == best else ""
        print(f"  eps={eps:<5} {val:.4f}{marker}")


def cmd_holdout(matrix_path: str, k: int, test_frac: float, queries_by_source: str | None, seed: int):
    from mwmbl.tinysearchengine.super_search_select.evaluation import (
        RewardMatrix,
        evaluate_holdout,
    )

    m = RewardMatrix.load(matrix_path)
    home_by_query = None
    if queries_by_source:
        by_source = json.loads(Path(queries_by_source).read_text())
        home_by_query = {q: source for source, qs in by_source.items() for q in qs}
    result = evaluate_holdout(m, k=k, test_frac=test_frac, seed=seed, home_by_query=home_by_query)
    print(f"train queries: {result['n_train_queries']}, test queries: {result['n_test_queries']}")
    print(f"xgb test RMSE: {result['rmse']:.4f}\n")
    for metric in ("coverage_at_k", "home_recall_at_k"):
        print(f"{metric} (k={k}):")
        for name, val in sorted(result[metric].items(), key=lambda kv: -kv[1]):
            print(f"  {name:14} {val:.4f}")
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build-matrix")
    p_build.add_argument("--queries", required=True, help="file with one query per line")
    p_build.add_argument("--out", default="eval_matrix")
    p_build.add_argument("--reward", choices=["survival", "judge"], default="survival")
    p_build.add_argument("--checkpoint", default=None, help="fetch checkpoint JSONL (default: <out>.fetch.jsonl)")

    p_gold = sub.add_parser("build-gold-matrix")
    p_gold.add_argument("--out", default="devdata/ss_gold_matrix")

    p_select = sub.add_parser("select")
    p_select.add_argument("--matrix", default="eval_matrix")
    p_select.add_argument("--k", type=int, default=10)

    p_sim = sub.add_parser("simulate")
    p_sim.add_argument("--matrix", default="eval_matrix")
    p_sim.add_argument("--k", type=int, default=10)

    p_sxgb = sub.add_parser("simulate-xgb")
    p_sxgb.add_argument("--matrix", default="eval_matrix")
    p_sxgb.add_argument("--k", type=int, default=10)
    p_sxgb.add_argument("--epsilons", type=float, nargs="+", default=[0.0, 0.05, 0.1, 0.2])
    p_sxgb.add_argument("--refit-every", type=int, default=200)
    p_sxgb.add_argument("--min-rows", type=int, default=300)

    p_hold = sub.add_parser("holdout")
    p_hold.add_argument("--matrix", default="eval_matrix")
    p_hold.add_argument("--k", type=int, default=10)
    p_hold.add_argument("--test-frac", type=float, default=0.2)
    p_hold.add_argument(
        "--queries-by-source", default=None, help="JSON {source: [queries]} home map (ss_source_queries.json)"
    )
    p_hold.add_argument("--seed", type=int, default=0)

    args = parser.parse_args()
    _bootstrap_django()

    if args.command == "build-matrix":
        queries = [ln.strip() for ln in Path(args.queries).read_text().splitlines() if ln.strip()]
        build_matrix(queries, args.out, reward=args.reward, checkpoint=args.checkpoint)
    elif args.command == "build-gold-matrix":
        build_gold_matrix(args.out)
    elif args.command == "select":
        cmd_select(args.matrix, args.k)
    elif args.command == "simulate":
        cmd_simulate(args.matrix, args.k)
    elif args.command == "simulate-xgb":
        cmd_simulate_xgb(args.matrix, args.k, args.epsilons, args.refit_every, args.min_rows)
    elif args.command == "holdout":
        cmd_holdout(args.matrix, args.k, args.test_frac, args.queries_by_source, args.seed)


if __name__ == "__main__":
    main()
