"""
LLM relabel — Pass 2 COLLECTION: build the candidate pool per query.

For each Pass-1 query, pool results from three sources, dedup by URL, and record
where each URL came from (provenance) so Pass-3 judging can stay blind:

- standard  : the production heuristic+LTR ranker (mwmbl.search_setup.ranker).
- supersearch: the real Super Search pipeline forced to query exactly the
               Pass-1-selected sources (fan-out + crawl + link-following). The
               url->source map (incl. inherited source for followed links) comes
               from the SelectionContext.
- google    : the scraped Google gold (rankings-train.csv), kept for blind
               calibration — NOT used as a label.

Output: append-only JSONL keyed by query (resumable), one record per query with
its pooled candidates. Slow + network-bound; run in the background and resume by
re-running (already-collected queries are skipped).

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        uv run python scripts/llm_relabel_pass2_collect.py --limit 5     # smoke
    ... uv run python scripts/llm_relabel_pass2_collect.py               # full run
    ... uv run python scripts/llm_relabel_pass2_collect.py --status
"""
import asyncio
import json
import os
from argparse import ArgumentParser

import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.conf import settings  # noqa: E402

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter  # noqa: E402
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex  # noqa: E402
from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.rank import HeuristicAndWikiRanker  # noqa: E402
from mwmbl.tinysearchengine.super_search import _run_pipeline  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.registry import get_meta  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext  # noqa: E402
from mwmbl.tinysearchengine.super_search_sources import SOURCES  # noqa: E402

PASS1 = "devdata/llm_relabel/pass1_intents.jsonl"
CHECKPOINT = "devdata/llm_relabel/pass2_pool.jsonl"

ALWAYS_ON = {name for name in SOURCES if get_meta(name).always_on}

# Standard/mwmbl pool: the same heuristic ranker over the production RemoteIndex
# that dataset.py uses to build the existing LTR data, so the relabel is a
# drop-in replacement (score_threshold=-inf keeps all candidates for pooling).
_std_ranker = HeuristicAndWikiRanker(
    RemoteIndex(), DummyCompleter(),
    return_none_if_no_mwmbl_results=True, score_threshold=float("-inf"),
    max_wiki_results=5,
)


async def _noop_emit(event_type, data):
    pass


async def _collect_ss(query: str, sources: list[str]) -> tuple[list[Document], dict]:
    """Run the SS pipeline forced to exactly ``sources`` (+ always-on); return
    the full document pool and the url->source provenance map."""
    settings.SUPER_SEARCH_FORCE_INCLUDE = list(sources)
    settings.SUPER_SEARCH_SOURCES_TO_QUERY = len(ALWAYS_ON | set(sources))
    all_docs: list[Document] = []
    ctx = SelectionContext()
    try:
        await asyncio.wait_for(
            _run_pipeline(query, _noop_emit, all_docs, [None], asyncio.Lock(), ctx),
            timeout=settings.SUPER_SEARCH_DEADLINE_SECONDS + 2,
        )
    except asyncio.TimeoutError:
        pass
    return all_docs, dict(ctx.source_by_url)


def _add(pool: dict, url: str, title: str, extract: str, state, score, *,
         pool_tag: str, ss_source: str | None = None, gold_rank: int | None = None):
    """Merge one result into the per-url pool, keeping the richest title/extract."""
    if not url:
        return
    item = pool.setdefault(url, {
        "url": url, "title": "", "extract": "", "state": None, "score": None,
        "pools": [], "ss_source": None, "gold_rank": None,
    })
    if title and len(title) > len(item["title"]):
        item["title"] = title
    if extract and len(extract) > len(item["extract"]):
        item["extract"] = extract
    if score is not None and item["score"] is None:
        item["score"] = float(score)
    if state is not None and item["state"] is None:
        item["state"] = state
    if pool_tag not in item["pools"]:
        item["pools"].append(pool_tag)
    if ss_source and not item["ss_source"]:
        item["ss_source"] = ss_source
    if gold_rank is not None and item["gold_rank"] is None:
        item["gold_rank"] = int(gold_rank)


def collect_query(query: str, sources: list[str], gold: pd.DataFrame, std_top_k: int) -> dict:
    pool: dict = {}
    gold_urls = set(gold["url"].tolist())

    # standard (production index, mirrors dataset.py: trailing space + " ").
    # The heuristic ranker returns the whole recall set; TREC-style we pool only
    # the top-K by score (what could realistically rank), but always keep any
    # gold URL as a calibration anchor.
    std = sorted((_std_ranker.search(query + " ", []) or []),
                 key=lambda d: d.score if d.score is not None else float("-inf"), reverse=True)
    kept = std[:std_top_k] + [d for d in std[std_top_k:] if d.url in gold_urls]
    for d in kept:
        _add(pool, d.url, d.title or "", d.extract or "", d.state, d.score, pool_tag="standard")

    # super search (forced sources) + provenance. Drop results from always-on
    # sources (e.g. hn) that Pass-1 did NOT choose: the pipeline pins them
    # regardless, but the pool should reflect only the chosen sources, so they
    # don't flood non-tech queries with off-topic noise.
    stray = ALWAYS_ON - set(sources)
    ss_docs, source_by_url = asyncio.run(_collect_ss(query, sources))
    for d in ss_docs:
        src = source_by_url.get(d.url)
        if src in stray:
            continue
        _add(pool, d.url, d.title or "", d.extract or "", d.state, d.score,
             pool_tag="supersearch", ss_source=src)

    # google gold (snippet only)
    for _, row in gold.iterrows():
        _add(pool, row["url"], "", str(row.get("snippet") or ""), None, None,
             pool_tag="google", gold_rank=row["rank"])

    return {"query": query, "sources": sources, "candidates": list(pool.values())}


def _load_pass1() -> dict:
    out = {}
    for line in open(PASS1):
        line = line.strip()
        if line:
            rec = json.loads(line)
            out[rec["query"]] = rec
    return out


def _done() -> set:
    if not os.path.exists(CHECKPOINT):
        return set()
    return {json.loads(l)["query"] for l in open(CHECKPOINT) if l.strip()}


def cmd_status():
    p1, done = _load_pass1(), _done()
    print(f"collected {len(done)} / {len(p1)} pass-1 queries ({CHECKPOINT})")
    if done:
        recs = [json.loads(l) for l in open(CHECKPOINT) if l.strip()]
        n_cand = sum(len(r["candidates"]) for r in recs)
        from collections import Counter
        pools = Counter(t for r in recs for c in r["candidates"] for t in c["pools"])
        print(f"  {n_cand} candidates total, {n_cand / max(len(recs),1):.1f} avg/query")
        print(f"  pool membership: {dict(pools)}")


def run(limit: int | None, std_top_k: int):
    p1 = _load_pass1()
    done = _done()
    todo = [q for q in p1 if q not in done]
    if limit:
        todo = todo[:limit]
    gold_all = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH, index_col=0)
    by_query = {q: g for q, g in gold_all.groupby("query")}
    print(f"collecting {len(todo)} queries (skipping {len(done)} done)")
    with open(CHECKPOINT, "a") as out:
        for i, query in enumerate(todo, 1):
            rec = collect_query(query, p1[query]["sources"],
                                by_query.get(query, gold_all.iloc[0:0]), std_top_k)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{i}/{len(todo)}] {query!r}: {len(rec['candidates'])} candidates", flush=True)


def main():
    parser = ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--std-top-k", type=int, default=30,
                        help="Pool only the top-K standard candidates by score (+ any gold URL).")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        cmd_status()
    else:
        run(args.limit, args.std_top_k)


if __name__ == "__main__":
    main()
