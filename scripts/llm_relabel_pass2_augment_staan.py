"""LLM relabel — Pass 2 AUGMENT: add Staan's results to an already-collected pool.

Combined Search pools the Mwmbl index, Staan and Wikipedia, so the model that ranks that
union has to be trained on it. The existing pool already covers the index and Wikipedia (both
arrive in the `standard` pool, via HeuristicAndWikiRanker); what it has never seen is Staan.

Re-running llm_relabel_pass2_collect.py would redo the whole Super Search fan-out — crawls,
link-following and all — for 849 queries to add one source. This adds only the Staan call,
and merges it into the existing pool through the same rule the other pools were built with
(scripts/_relabel_pool.add), so a Staan row is comparable with an existing one.

Two steps, so a long network-bound run can never leave the pool half-written:

1. ``--collect`` (default) appends one record per query to a checkpoint of its own. Resumable:
   re-running skips queries already collected.
2. ``--merge`` folds the checkpoint into pass2_pool.jsonl, writing via a temporary file and
   renaming, so the pool is either the old one or the new one and never a partial one.

The score written for a Staan result is staan_score(rank) — the same constant the serving
path uses. `score` becomes the item_score feature, so a scale here that production never
produces is a model trained on a feature it will not see. See STAAN_TOP_SCORE.

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        uv run python scripts/llm_relabel_pass2_augment_staan.py --limit 5    # smoke
    ... uv run python scripts/llm_relabel_pass2_augment_staan.py              # full run
    ... uv run python scripts/llm_relabel_pass2_augment_staan.py --status
    ... uv run python scripts/llm_relabel_pass2_augment_staan.py --merge
"""

import json
import os
from argparse import ArgumentParser
from collections import Counter

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from mwmbl.indexer.external_cache import get_cached_external_results  # noqa: E402
from mwmbl.tinysearchengine.indexer import DocumentSource  # noqa: E402
from mwmbl.tinysearchengine.staan import get_staan_results  # noqa: E402
from scripts._relabel_pool import add  # noqa: E402

POOL = "devdata/llm_relabel/pass2_pool.jsonl"
CHECKPOINT = "devdata/llm_relabel/pass2_staan.jsonl"
POOL_TAG = "staan"


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path) if line.strip()]


def _pool_queries() -> list[str]:
    return [record["query"] for record in _read_jsonl(POOL)]


def _collected() -> set[str]:
    return {record["query"] for record in _read_jsonl(CHECKPOINT)}


def collect_query(query: str) -> tuple[dict, bool]:
    """Staan's results for one query, and whether the provider actually answered.

    get_staan_results returns [] both for "Staan has nothing for this" and for "the request
    failed", and the difference matters here: the checkpoint is the resume marker, so
    recording a failure as an empty result freezes it in and the query is never retried.

    The cache tells the two apart, because that is exactly the distinction it exists to
    keep - a well-formed empty answer is stored as the sentinel, a failure is not stored at
    all. So a miss straight after the call means the fetch failed.
    """
    results = get_staan_results(query)
    answered = get_cached_external_results(DocumentSource.STAAN, query) is not None
    record = {
        "query": query,
        "results": [
            {"url": document.url, "title": document.title, "extract": document.extract, "score": document.score}
            for document in results
        ],
    }
    return record, answered


def cmd_collect(limit: int | None):
    todo = [query for query in _pool_queries() if query not in _collected()]
    if limit:
        todo = todo[:limit]
    print(f"Collecting Staan results for {len(todo)} queries")

    failed = 0
    with open(CHECKPOINT, "a") as checkpoint:
        for i, query in enumerate(todo, 1):
            record, answered = collect_query(query)
            if not answered:
                # Left out of the checkpoint on purpose, so re-running picks it up again.
                failed += 1
                print(f"[{i}/{len(todo)}] {query!r}: FETCH FAILED, not checkpointed")
                continue
            checkpoint.write(json.dumps(record) + "\n")
            checkpoint.flush()
            print(f"[{i}/{len(todo)}] {query!r}: {len(record['results'])} results")

    if failed:
        print(f"\n{failed} queries failed to fetch and were not checkpointed. Re-run to retry them.")


def cmd_merge():
    collected = {record["query"]: record["results"] for record in _read_jsonl(CHECKPOINT)}
    if not collected:
        print(f"Nothing to merge: {CHECKPOINT} is empty")
        return

    added = 0
    merged_into_existing = 0
    records = _read_jsonl(POOL)
    for record in records:
        results = collected.get(record["query"])
        if results is None:
            continue
        pool = {candidate["url"]: candidate for candidate in record["candidates"]}
        for result in results:
            existing = result["url"] in pool
            add(
                pool,
                result["url"],
                result["title"] or "",
                result["extract"] or "",
                None,
                result["score"],
                pool_tag=POOL_TAG,
                ss_source=POOL_TAG,
            )
            if existing:
                merged_into_existing += 1
            else:
                added += 1
        record["candidates"] = list(pool.values())

    temporary = POOL + ".tmp"
    with open(temporary, "w") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    os.replace(temporary, POOL)

    print(f"Merged {len(collected)} queries into {POOL}")
    print(f"  {added} new candidates, {merged_into_existing} already pooled by another source")


def cmd_status():
    queries = _pool_queries()
    collected = _read_jsonl(CHECKPOINT)
    print(f"collected {len(collected)} / {len(queries)} pool queries ({CHECKPOINT})")
    if collected:
        counts = [len(record["results"]) for record in collected]
        print(f"  {sum(counts)} Staan results, {sum(counts) / len(counts):.1f} avg/query")
        print(f"  queries with no Staan results: {sum(1 for c in counts if c == 0)}")

    pools = Counter(tag for record in _read_jsonl(POOL) for c in record["candidates"] for tag in c["pools"])
    print(f"  pool membership in {POOL}: {dict(pools)}")


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Collect at most this many queries.")
    parser.add_argument("--merge", action="store_true", help="Fold the checkpoint into pass2_pool.jsonl.")
    parser.add_argument("--status", action="store_true", help="Report progress and pool membership.")
    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.merge:
        cmd_merge()
    else:
        cmd_collect(args.limit)


if __name__ == "__main__":
    run()
