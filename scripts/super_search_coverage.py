#!/usr/bin/env python3
"""Phase 1 feasibility gate for offline gold-grounded Super Search source selection.

Measures how much the rankeval *gold* dataset overlaps our Super Search source
domains. Source selection can only move NDCG on queries where a gold URL lives on
one of our source domains, so this script quantifies that ceiling before we build
the gold-grounded reward matrix / selector.

For every gold URL we match its host against the ~130 registered source domains
under two normalizations:

  * exact host   - the host string equals a source domain. This is the only kind
                   of match that can actually score in the NDCG harness, which
                   compares URLs by exact string (evaluate.py: ``scores.get(url)``).
  * registrable  - strip ``www.``/``m.`` and compare registrable domains. An upper
                   bound on recoverable coverage if we fixed URL normalization
                   (over-counts sibling subdomains, e.g. support. vs developer.).

Run:
  DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
      uv run python scripts/super_search_coverage.py
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import django
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from mwmbl.rankeval.evaluation.evaluate import CLICK_PROPORTIONS, NUM_RESULTS_FOR_EVAL  # noqa: E402
from mwmbl.rankeval.paths import (  # noqa: E402
    RANKINGS_DATASET_TEST_PATH,
    RANKINGS_DATASET_TRAIN_PATH,
)
from mwmbl.tinysearchengine.super_search_select.domains import host_of, registrable  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.features import (  # noqa: E402
    INTENT_NAMES,
    classify_intent,
)
from mwmbl.tinysearchengine.super_search_select.registry import get_registry  # noqa: E402


def load_gold() -> pd.DataFrame:
    frames = []
    for split, path in (("train", RANKINGS_DATASET_TRAIN_PATH),
                        ("test", RANKINGS_DATASET_TEST_PATH)):
        df = pd.read_csv(path)
        df["split"] = split
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def click_weight(rank) -> float:
    """Gold relevance mass of a row by its 1-based rank (0 beyond the top-10)."""
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return 0.0
    return CLICK_PROPORTIONS[r - 1] if 1 <= r <= NUM_RESULTS_FOR_EVAL else 0.0


def cmd_report():
    registry = get_registry()
    # source domain (lowercased) -> source names; same keyed by registrable domain.
    exact_map: dict[str, list[str]] = defaultdict(list)
    reg_map: dict[str, list[str]] = defaultdict(list)
    field_of: dict[str, str] = {}
    for name, meta in registry.items():
        domain = meta.domain.lower()
        exact_map[domain].append(name)
        reg_map[registrable(domain)].append(name)
        field_of[name] = meta.field

    gold = load_gold()
    n_rows = len(gold)
    queries = gold["query"].unique()
    n_queries = len(queries)

    # Per-query matched source-name sets, under each normalization.
    q_exact: dict[str, set[str]] = defaultdict(set)
    q_reg: dict[str, set[str]] = defaultdict(set)
    exact_rows = reg_rows = 0
    exact_mass = total_mass = 0.0
    rank_hist_exact: Counter = Counter()        # rank -> hit count (exact)
    source_queries_exact: dict[str, set[str]] = defaultdict(set)
    source_queries_reg: dict[str, set[str]] = defaultdict(set)

    for query, url, rank in zip(gold["query"], gold["url"], gold["rank"]):
        w = click_weight(rank)
        total_mass += w
        host = host_of(url)
        if not host:
            continue
        ex = exact_map.get(host)
        rg = reg_map.get(registrable(host))
        if ex:
            exact_rows += 1
            exact_mass += w
            try:
                rank_hist_exact[int(rank)] += 1
            except (TypeError, ValueError):
                pass
            for name in ex:
                q_exact[query].add(name)
                source_queries_exact[name].add(query)
        if rg:
            reg_rows += 1
            for name in rg:
                q_reg[query].add(name)
                source_queries_reg[name].add(query)

    n_q_exact = len(q_exact)
    n_q_reg = len(q_reg)

    def pct(num, den):
        return f"{100.0 * num / den:.2f}%" if den else "n/a"

    print("=" * 72)
    print("Super Search gold-coverage feasibility gate")
    print("=" * 72)
    print(f"Sources registered:        {len(registry)}")
    print(f"Distinct source domains:   {len(exact_map)} exact / {len(reg_map)} registrable")
    print(f"Gold rows (train+test):    {n_rows}")
    print(f"Gold queries:              {n_queries}")
    print()
    print("Row-level overlap (a gold URL on a source domain):")
    print(f"  exact host:       {exact_rows:>6} rows  ({pct(exact_rows, n_rows)})")
    print(f"  registrable:      {reg_rows:>6} rows  ({pct(reg_rows, n_rows)})")
    print()
    print("Query-level coverage (>=1 gold URL on a source domain)  <-- the ceiling:")
    print(f"  exact host:       {n_q_exact:>6} queries ({pct(n_q_exact, n_queries)})")
    print(f"  registrable:      {n_q_reg:>6} queries ({pct(n_q_reg, n_queries)})")
    print()
    print("Gold relevance mass (click-weighted, top-10 only) on source domains:")
    print(f"  exact host:       {exact_mass:.1f} / {total_mass:.1f}  ({pct(exact_mass, total_mass)})")
    print()

    print("-" * 72)
    print("Per-source covered queries (exact host; registrable in parens):")
    print("-" * 72)
    ranked = sorted(source_queries_exact.items(), key=lambda kv: -len(kv[1]))
    # include reg-only sources (matched under registrable but never exact) at the tail
    reg_only = [n for n in source_queries_reg if n not in source_queries_exact]
    for name in reg_only:
        ranked.append((name, set()))
    for name, qs in ranked:
        reg_n = len(source_queries_reg.get(name, set()))
        domain = registry[name].domain if name in registry else name
        print(f"  {name:24} {domain:28} {len(qs):>4} ({reg_n:>4})")
    print()

    print("-" * 72)
    print("Gold-rank distribution of exact-host hits (where the source URL ranks):")
    print("-" * 72)
    buckets = {"rank 1-3": 0, "rank 4-10": 0, "rank 11+": 0}
    for rank, count in rank_hist_exact.items():
        if 1 <= rank <= 3:
            buckets["rank 1-3"] += count
        elif 4 <= rank <= 10:
            buckets["rank 4-10"] += count
        else:
            buckets["rank 11+"] += count
    for label, count in buckets.items():
        print(f"  {label:12} {count:>6} ({pct(count, exact_rows)})")
    print()

    print("-" * 72)
    print("Stratification by source field (exact-host covered query-field pairs):")
    print("-" * 72)
    field_cov: Counter = Counter()
    for query, names in q_exact.items():
        for field in {field_of.get(n, "other") for n in names}:
            field_cov[field] += 1
    for field, count in field_cov.most_common():
        print(f"  {field:24} {count:>5} queries")
    print()

    print("-" * 72)
    print("Stratification by query intent (coverage rate among queries of that intent):")
    print("-" * 72)
    intent_total: Counter = Counter()
    intent_covered: Counter = Counter()
    covered_queries = set(q_exact)
    for query in queries:
        intents = classify_intent(query)
        for name, flag in zip(INTENT_NAMES, intents):
            if flag:
                intent_total[name] += 1
                if query in covered_queries:
                    intent_covered[name] += 1
    for name in INTENT_NAMES:
        tot = intent_total[name]
        cov = intent_covered[name]
        print(f"  {name:12} {cov:>5}/{tot:<6} covered ({pct(cov, tot)})")
    print()
    print("=" * 72)
    print("GATE: query-level coverage is the hard ceiling on how many queries source")
    print("selection can affect. If even the registrable number stays in low single")
    print("digits, prefer a static best-sources list over a learned per-query model.")
    print("=" * 72)


def _norm(url: str) -> tuple[str, str]:
    """Registrable-host + path (trailing slash / query stripped) for near-match."""
    return registrable(host_of(url)), urlparse(str(url)).path.rstrip("/")


def cmd_recall(source: str, per_source_limit: int, max_queries: int):
    """For one source, measure how many of the gold URLs on its domain it actually
    returns. exact-URL recall is what scores in NDCG (evaluate.py exact match);
    near-match recall (host+path, ignoring www/m/trailing-slash/query) shows whether
    a URL-format fix would recover the rest."""
    import asyncio

    import httpx
    from django.conf import settings

    from mwmbl.tinysearchengine.super_search_select.registry import get_meta
    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    if source not in SOURCES:
        print(f"unknown source {source!r}. Known (sample): {sorted(SOURCES)[:8]}...")
        return
    dom = registrable(get_meta(source).domain.lower())
    gold = load_gold()
    targets: dict[str, set[str]] = defaultdict(set)
    for query, url in zip(gold["query"], gold["url"]):
        if registrable(host_of(str(url))) == dom:
            targets[query].add(str(url))
    qs = list(targets)
    print(f"source {source!r} (registrable domain {dom}): {len(qs)} gold queries touch it")
    if max_queries and len(qs) > max_queries:
        import random as _random
        _random.Random(0).shuffle(qs)
        qs = qs[:max_queries]
        print(f"sampling {len(qs)} of them")

    fn = SOURCES[source]
    timeout = max(float(settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT), 10.0)

    async def run() -> dict[str, list[str]]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     headers={"User-Agent": "mwmbl-super-search-eval/0.1"}) as client:
            out: dict[str, list[str]] = {}
            for q in qs:
                try:
                    docs = await fn(client, q, per_source_limit)
                except Exception:
                    docs = []
                out[q] = [d.url for d in docs if d.url]
            return out

    returned = asyncio.run(run())
    exact = near = empty = 0
    misses = []
    for q in qs:
        tgt = targets[q]
        ret = returned.get(q, [])
        if not ret:
            empty += 1
        rset, rnorm = set(ret), {_norm(u) for u in ret}
        ex = any(t in rset for t in tgt)
        nr = any(_norm(t) in rnorm for t in tgt)
        exact += ex
        near += nr
        if not ex and len(misses) < 10:
            misses.append((q, sorted(tgt)[0], ret[:2]))
    n = len(qs) or 1
    print(f"\nexact-URL recall:           {exact}/{len(qs)} ({100*exact/n:.1f}%)")
    print(f"near-match recall (host+path): {near}/{len(qs)} ({100*near/n:.1f}%)")
    print(f"queries where source returned nothing: {empty}/{len(qs)}")
    print("\nmisses (gold target vs our top returns):")
    for q, t, ret in misses:
        print(f"  q={q!r}\n     gold: {t}\n     ours: {ret}")


def cmd_missing(top: int):
    """Rank gold domains we do NOT have as a source by click-weighted gold mass —
    i.e. the addressable upside from adding new sources."""
    ours = {registrable(m.domain.lower()) for m in get_registry().values()}
    gold = load_gold()
    mass: dict[str, float] = defaultdict(float)
    queries: dict[str, set] = defaultdict(set)
    rows: Counter = Counter()
    top3: Counter = Counter()
    total = 0.0
    for query, url, rank in zip(gold["query"], gold["url"], gold["rank"]):
        w = click_weight(rank)
        total += w
        host = host_of(url)
        if not host:
            continue
        dom = registrable(host)
        mass[dom] += w
        queries[dom].add(query)
        rows[dom] += 1
        try:
            if 1 <= int(rank) <= 3:
                top3[dom] += 1
        except (TypeError, ValueError):
            pass

    miss = [(d, m, len(queries[d]), rows[d], top3[d]) for d, m in mass.items() if d not in ours]
    miss.sort(key=lambda x: -x[1])
    ours_mass = sum(m for d, m in mass.items() if d in ours)
    print(f"total gold mass {total:.0f} | distinct domains {len(mass)} | missing {len(miss)}")
    print(f"domains already a source (registrable match) = {100*ours_mass/total:.2f}% of gold mass\n")
    print(f"{'#':>3} {'domain':32} {'mass':>7} {'%mass':>6} {'queries':>8} {'rows':>6} {'rank1-3':>7}")
    for i, (d, m, nq, nr, t3) in enumerate(miss[:top]):
        print(f"{i+1:>3} {d:32} {m:7.1f} {100*m/total:5.1f}% {nq:8} {nr:6} {t3:7}")
    print()
    for n in (10, 20, 40):
        share = 100 * sum(x[1] for x in miss[:n]) / total
        print(f"top-{n} missing domains = {share:.1f}% of gold mass")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("report", help="gold-coverage feasibility report (default)")
    sub.add_parser("missing", help="rank gold domains we lack by gold mass")
    p_missing = sub.choices["missing"]
    p_missing.add_argument("--top", type=int, default=45)
    p_recall = sub.add_parser("recall", help="per-source exact-URL recall of gold")
    p_recall.add_argument("--source", required=True, help="source name in SOURCES")
    p_recall.add_argument("--limit", type=int, default=10)
    p_recall.add_argument("--max-queries", type=int, default=80)
    args = parser.parse_args()
    if args.command == "recall":
        cmd_recall(args.source, args.limit, args.max_queries)
    elif args.command == "missing":
        cmd_missing(args.top)
    else:
        cmd_report()


if __name__ == "__main__":
    main()
