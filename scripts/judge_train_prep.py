#!/usr/bin/env python3
"""Build frozen train/val/eval splits for fine-tuning the relevance judge.

Inputs:
- devdata/rankeval-2026-04/learning-to-rank-llm.csv.gz (LLM-graded pointwise
  labels; loaded via judge_bakeoff.load_dataset so row indexes align with the
  cached zero-shot score arrays in devdata/judge_bakeoff/)
- devdata/judgments_export/pairs.jsonl.gz (human preference pairs)

Splits are deterministic (md5 of the normalized query), by query, never by row:
- LLM queries: train 40 / val 10 / eval 50, stratified on source-eligibility
  (>=2 supersearch sources with >=2 rows — the bandit's consumption shape).
  Eval is deliberately large: only ~237 source-eligible queries exist and the
  source-level metrics are the decision criterion for the fine-tune.
- Pair queries: train 85 / val 5 / eval 10. Any pair whose query appears in the
  LLM val/eval sets is dropped from all pair splits (leakage guard).

Pair filtering: both sides need title+extract (no URL-as-text fallback unless
--url-fallback; URL tokens are a spurious serve-time feature), dedup on
(query, pos.url, neg.url), cap pairs per query, cap any single user's share of
the train split (one power curator contributes ~39% of all pairs).

Outputs (devdata/judge_train/, refuses to overwrite without --force):
- pointwise_{train,val}.jsonl.gz   {query, doc_text, label in [0,1]}
- pairs_{train,val,eval}.jsonl.gz  {query, pos, neg, rule, user} (text only)
- eval_manifest.json               frozen contract: seed, splits, LLM eval row
                                   indexes + queries, input sha256s

Run:  uv run python scripts/judge_train_prep.py [--force]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import judge_bakeoff

PAIRS_FILE = Path("devdata/judgments_export/pairs.jsonl.gz")
OUT_DIR = Path("devdata/judge_train")

SEED = 42
LLM_BUCKETS = (40, 10, 50)  # train / val / eval, per 100
PAIR_BUCKETS = (85, 5, 10)
PAIR_CAP_PER_QUERY = 16
USER_TRAIN_SHARE_CAP = 0.3
LABEL_SCALE = 10.0  # `overall` is 0-10 -> [0, 1]


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())


def query_bucket(query: str) -> int:
    return int(hashlib.md5(normalize_query(query).encode()).hexdigest(), 16) % 100


def split_queries(queries: set[str], buckets: tuple[int, int, int]) -> dict[str, str]:
    """Assign each query to train/val/eval by ordering on md5 within the set.

    Ordering by hash (rather than thresholding the hash directly) gives exact
    fractions within each stratum, which matters when a stratum has only a few
    hundred queries. Deterministic for a fixed query set; the manifest freezes
    the input files so the set is fixed.
    """
    ordered = sorted(queries, key=lambda q: hashlib.md5(normalize_query(q).encode()).hexdigest())
    n = len(ordered)
    train_end = round(n * buckets[0] / 100)
    val_end = train_end + round(n * buckets[1] / 100)
    assignment = {}
    for i, query in enumerate(ordered):
        assignment[query] = "train" if i < train_end else "val" if i < val_end else "eval"
    return assignment


def source_eligible_queries(rows: list[dict]) -> set[str]:
    """Queries usable for source-level metrics (same shape as judge_bakeoff.evaluate)."""
    sources = defaultdict(Counter)
    for row in rows:
        if row["ss_source"] and "supersearch" in row["pools"]:
            sources[row["query"]][row["ss_source"]] += 1
    return {query for query, counts in sources.items() if sum(count >= 2 for count in counts.values()) >= 2}


def load_pairs() -> list[dict]:
    with gzip.open(PAIRS_FILE, "rt") as f:
        return [json.loads(line) for line in f]


def doc_text(doc: dict, url_fallback: bool) -> str | None:
    title, extract = doc.get("title") or "", doc.get("extract") or ""
    if title and extract:
        return f"{title}. {extract}"[: judge_bakeoff.DOC_CHARS]
    return doc["url"][: judge_bakeoff.DOC_CHARS] if url_fallback else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_jsonl_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    print(f"  wrote {path} ({len(records)} records)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="overwrite an existing devdata/judge_train/")
    parser.add_argument(
        "--url-fallback", action="store_true", help="use the URL as doc text when title/extract missing"
    )
    args = parser.parse_args()

    if OUT_DIR.exists() and any(OUT_DIR.iterdir()) and not args.force:
        sys.exit(
            f"{OUT_DIR} already exists; pass --force to regenerate "
            "(this invalidates any models trained on the old split)"
        )

    rng = random.Random(SEED)

    # --- LLM set: pointwise labels + frozen eval row indexes ------------------
    rows = judge_bakeoff.load_dataset()
    queries = {row["query"] for row in rows}
    eligible = source_eligible_queries(rows)
    print(f"LLM set: {len(rows)} rows, {len(queries)} queries, {len(eligible)} source-eligible")

    llm_split = {}
    llm_split.update(split_queries(eligible, LLM_BUCKETS))
    llm_split.update(split_queries(queries - eligible, LLM_BUCKETS))

    pointwise = {"train": [], "val": []}
    eval_row_indexes = []
    for i, row in enumerate(rows):
        part = llm_split[row["query"]]
        if part == "eval":
            eval_row_indexes.append(i)
        else:
            pointwise[part].append(
                {
                    "query": row["query"],
                    "doc_text": row["doc_text"],
                    "label": row["overall"] / LABEL_SCALE,
                }
            )

    split_queries_by_part = defaultdict(set)
    for query, part in llm_split.items():
        split_queries_by_part[part].add(query)
    for part in ("train", "val", "eval"):
        n_eligible = len(split_queries_by_part[part] & eligible)
        print(f"  llm {part}: {len(split_queries_by_part[part])} queries ({n_eligible} source-eligible)")

    # --- preference pairs ------------------------------------------------------
    raw_pairs = load_pairs()
    llm_heldout = {normalize_query(q) for q in split_queries_by_part["val"] | split_queries_by_part["eval"]}

    pairs, seen = [], set()
    dropped = Counter()
    for pair in raw_pairs:
        pos_text = doc_text(pair["pos"], args.url_fallback)
        neg_text = doc_text(pair["neg"], args.url_fallback)
        if pos_text is None or neg_text is None:
            dropped["missing_text"] += 1
            continue
        key = (normalize_query(pair["query"]), pair["pos"]["url"], pair["neg"]["url"])
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        if key[0] in llm_heldout:
            dropped["llm_heldout_leakage"] += 1
            continue
        pairs.append(
            {"query": pair["query"], "pos": pos_text, "neg": neg_text, "rule": pair["rule"], "user": pair["user"]}
        )
    print(f"pairs: {len(raw_pairs)} raw -> {len(pairs)} kept, dropped {dict(dropped)}")

    pair_split = split_queries({pair["query"] for pair in pairs}, PAIR_BUCKETS)
    pair_parts = {"train": [], "val": [], "eval": []}
    for pair in pairs:
        pair_parts[pair_split[pair["query"]]].append(pair)

    # per-query cap on train+val (eval keeps its natural distribution)
    for part in ("train", "val"):
        by_query = defaultdict(list)
        for pair in pair_parts[part]:
            by_query[pair["query"]].append(pair)
        capped = []
        for query_pairs in by_query.values():
            if len(query_pairs) > PAIR_CAP_PER_QUERY:
                query_pairs = rng.sample(query_pairs, PAIR_CAP_PER_QUERY)
            capped.extend(query_pairs)
        print(f"  pairs {part}: {len(pair_parts[part])} -> {len(capped)} after per-query cap")
        pair_parts[part] = capped

    # per-user share cap on train only
    train = pair_parts["train"]
    user_counts = Counter(pair["user"] for pair in train)
    top_user, top_count = user_counts.most_common(1)[0]
    max_share = top_count / len(train)
    if max_share > USER_TRAIN_SHARE_CAP:
        # solve keep so that keep / (len(train) - top_count + keep) == cap
        others = len(train) - top_count
        keep = int(USER_TRAIN_SHARE_CAP * others / (1 - USER_TRAIN_SHARE_CAP))
        top_pairs = [p for p in train if p["user"] == top_user]
        kept = set(map(id, rng.sample(top_pairs, keep)))
        train = [p for p in train if p["user"] != top_user or id(p) in kept]
        print(
            f"  pairs train: capped {top_user} {top_count} -> {keep} "
            f"({max_share:.0%} -> {keep / len(train):.0%}); total {len(train)}"
        )
        pair_parts["train"] = train

    for part, part_pairs in pair_parts.items():
        rule_counts = Counter(pair["rule"] for pair in part_pairs)
        print(
            f"  pairs {part}: {len(part_pairs)} pairs, "
            f"{len({p['query'] for p in part_pairs})} queries, rules {dict(rule_counts)}"
        )

    # --- write outputs ---------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl_gz(OUT_DIR / "pointwise_train.jsonl.gz", pointwise["train"])
    write_jsonl_gz(OUT_DIR / "pointwise_val.jsonl.gz", pointwise["val"])
    for part in ("train", "val", "eval"):
        write_jsonl_gz(OUT_DIR / f"pairs_{part}.jsonl.gz", pair_parts[part])

    manifest = {
        "seed": SEED,
        "llm_buckets": LLM_BUCKETS,
        "pair_buckets": PAIR_BUCKETS,
        "pair_cap_per_query": PAIR_CAP_PER_QUERY,
        "user_train_share_cap": USER_TRAIN_SHARE_CAP,
        "url_fallback": args.url_fallback,
        "llm_eval_row_indexes": eval_row_indexes,
        "llm_eval_queries": sorted(split_queries_by_part["eval"]),
        "llm_val_queries": sorted(split_queries_by_part["val"]),
        "llm_train_query_count": len(split_queries_by_part["train"]),
        "source_eligible_eval_queries": len(split_queries_by_part["eval"] & eligible),
        "pair_counts": {part: len(pair_parts[part]) for part in pair_parts},
        "inputs_sha256": {
            str(judge_bakeoff.DATASET): sha256_file(judge_bakeoff.DATASET),
            str(PAIRS_FILE): sha256_file(PAIRS_FILE),
        },
    }
    (OUT_DIR / "eval_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"  wrote {OUT_DIR / 'eval_manifest.json'} "
        f"({len(eval_row_indexes)} eval rows, "
        f"{manifest['source_eligible_eval_queries']} source-eligible eval queries)"
    )


if __name__ == "__main__":
    main()
