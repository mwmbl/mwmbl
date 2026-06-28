#!/usr/bin/env python3
"""Offline evaluation harness for Super Search source selection.

Subcommands:

  build-matrix  - query every source for each query in a query file, rank the
                  union with the LTR model, and write a dense (queries x sources)
                  reward + feature matrix. Requires network + Django.

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

Usage:
  DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python scripts/super_search_eval.py \
      build-matrix --queries queries.txt --out eval_matrix
  uv run python scripts/super_search_eval.py select   --matrix eval_matrix
  uv run python scripts/super_search_eval.py simulate --matrix eval_matrix
"""
import argparse
import asyncio
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
    host_of, registrable,
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
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": "mwmbl-super-search-eval/0.1"}) as client:
        async def one(name, fn):
            try:
                docs = await asyncio.wait_for(fn(client, query, limit), timeout=timeout)
                return name, docs
            except Exception:
                return name, []
        results = await asyncio.gather(*[one(n, f) for n, f in SOURCES.items()])
    return dict(results)


def build_matrix(queries: list[str], out: str):
    from django.conf import settings
    from mwmbl.search_setup import ltr_model
    from mwmbl.tinysearchengine.ltr_rank import score_documents
    from mwmbl.tinysearchengine.super_search_sources import SOURCES
    from mwmbl.tinysearchengine.super_search_select import profiles, vectors
    from mwmbl.tinysearchengine.super_search_select.features import (
        FEATURE_NAMES, QueryContext, feature_vector,
    )
    from mwmbl.tinysearchengine.super_search_select.registry import get_meta
    from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix

    sources = list(SOURCES.keys())
    s_index = {name: i for i, name in enumerate(sources)}
    limit = settings.SUPER_SEARCH_RESULTS_PER_SOURCE
    top_k = settings.SUPER_SEARCH_TOP_K
    dim = settings.SUPER_SEARCH_PROJECTION_DIM
    F = len(FEATURE_NAMES)

    # Pass 1: query everything, recording per-query docs and accumulating each
    # site's content profile (mean of its projected result samples).
    per_query_docs = []   # list[dict[source -> list[Document]]]
    prof_bow = {n: np.zeros(dim) for n in sources}
    prof_cng = {n: np.zeros(dim) for n in sources}
    for qi, query in enumerate(queries):
        docs_by_source = asyncio.run(_query_all_sources(query, limit))
        per_query_docs.append(docs_by_source)
        for name, docs in docs_by_source.items():
            if docs:
                text = profiles.sample_text(docs)
                prof_bow[name] += vectors.project_bow(text, dim)
                prof_cng[name] += vectors.project_char_ngrams(text, dim)
        print(f"  [{qi + 1}/{len(queries)}] {query!r}: "
              f"{sum(len(d) for d in docs_by_source.values())} docs")
    profile = {n: (vectors._l2_normalise(prof_bow[n]), vectors._l2_normalise(prof_cng[n]))
               for n in sources}

    # Pass 2: features + implicit-contribution reward against the LTR top-K.
    Q, S = len(queries), len(sources)
    X = np.zeros((Q, S, F))
    R = np.zeros((Q, S))
    mask = np.zeros((Q, S), dtype=bool)
    for qi, (query, docs_by_source) in enumerate(zip(queries, per_query_docs)):
        bow = vectors.project_bow(query, dim)
        cng = vectors.project_char_ngrams(query, dim)
        qctx = QueryContext.build(query, bow, cng)

        source_by_url, all_docs = {}, []
        for name, docs in docs_by_source.items():
            si = s_index[name]
            X[qi, si] = feature_vector(qctx, get_meta(name), profile[name])
            if docs:
                mask[qi, si] = True
            for d in docs:
                if d.url and d.title:
                    source_by_url.setdefault(d.url, name)
                    all_docs.append(d)
        if all_docs:
            scores = score_documents(ltr_model, query, all_docs)
            ranked = [d.url for d, _ in sorted(zip(all_docs, scores), key=lambda x: -x[1])][:top_k]
            counts = {}
            for url in ranked:
                src = source_by_url.get(url)
                if src:
                    counts[src] = counts.get(src, 0) + 1
            for name, c in counts.items():
                R[qi, s_index[name]] = min(c / max(limit, 1), 1.0)

    matrix = RewardMatrix(queries=queries, sources=sources,
                          feature_names=list(FEATURE_NAMES), X=X, R=R, mask=mask)
    matrix.save(out)
    print(f"Wrote matrix {out}.npz/.json: {Q} queries x {S} sources, "
          f"{int(mask.sum())} filled cells.")


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
    per_query: dict[str, dict[str, bool]] = {}   # query -> {source -> has_gold}
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
    from mwmbl.tinysearchengine.super_search_select.features import (
        FEATURE_NAMES, QueryContext, feature_vector,
    )
    from mwmbl.tinysearchengine.super_search_select.registry import get_meta, get_registry
    from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix

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

    matrix = RewardMatrix(queries=in_cov_queries, sources=sources,
                          feature_names=list(FEATURE_NAMES), X=X, R=R, mask=mask)
    matrix.save(out)

    # Diagnostics: does selection even have room to matter? If most queries have
    # few in-source candidates, "query them all" is as good as any learned policy.
    avail_per_q = mask.sum(axis=1)
    gold_per_q = (R > 0).sum(axis=1)
    n_gold_q = int((gold_per_q > 0).sum())
    print(f"\nWrote matrix {out}.npz/.json: {Q} in-coverage queries x {S} sources, "
          f"{int(mask.sum())} filled cells.")
    print(f"  queries with >=1 gold source:   {n_gold_q} ({100*n_gold_q/max(Q,1):.1f}%)")
    print(f"  mean available sources / query: {avail_per_q.mean():.2f} "
          f"(median {int(np.median(avail_per_q))}, max {int(avail_per_q.max())})")
    for k in (1, 2, 3, 5, 10):
        n = int((avail_per_q > k).sum())
        print(f"  queries with > {k:>2} available sources: {n:>4} "
              f"({100*n/max(Q,1):.1f}%)  <- room for top-{k} selection to matter")


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
        RewardMatrix, simulate_baselines, sweep_explore_scale,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build-matrix")
    p_build.add_argument("--queries", required=True, help="file with one query per line")
    p_build.add_argument("--out", default="eval_matrix")

    p_gold = sub.add_parser("build-gold-matrix")
    p_gold.add_argument("--out", default="devdata/ss_gold_matrix")

    p_select = sub.add_parser("select")
    p_select.add_argument("--matrix", default="eval_matrix")
    p_select.add_argument("--k", type=int, default=10)

    p_sim = sub.add_parser("simulate")
    p_sim.add_argument("--matrix", default="eval_matrix")
    p_sim.add_argument("--k", type=int, default=10)

    args = parser.parse_args()
    _bootstrap_django()

    if args.command == "build-matrix":
        queries = [ln.strip() for ln in Path(args.queries).read_text().splitlines() if ln.strip()]
        build_matrix(queries, args.out)
    elif args.command == "build-gold-matrix":
        build_gold_matrix(args.out)
    elif args.command == "select":
        cmd_select(args.matrix, args.k)
    elif args.command == "simulate":
        cmd_simulate(args.matrix, args.k)


if __name__ == "__main__":
    main()
