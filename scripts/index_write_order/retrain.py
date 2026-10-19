"""Retrain the query-time combined model on pools drawn from an arm's index.

#463 found that the crawl-domain write-time order keeps more good pages, but the shipped
query-time model, trained on pools from the heuristic's index, outranks them. This tests
whether retraining on the new index's pools fixes that. The training index is built by
build_corpus.py and build_index.py with INDEX_WRITE_ORDER_QUERIES=train: the same corpus
sources, filed under the lookup terms of the combined model's 849 LLM-labelled queries, none
of which is an eval query.

    pool ARM...     each training query's index-only candidates from the arm's training
                    index: the shipped combined model's top POOL_DEPTH and the heuristic's,
                    the way Pass 2 pooled the production index.
    batches         the pooled (query, url) pairs with no Pass-3 judgment yet, as chunks of
                    the Pass-3 prompt for Claude Haiku 4.5 judges.
    merge           the judges' output into judgments.jsonl.
    train NAME [ARM...]  the combined model's config (provider_features.py), plus the
                    named arms' pooled rows, into models/query-NAME.xgb.

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" PYTHONPATH=scripts/index_write_order:. \
        uv run python scripts/index_write_order/retrain.py pool heuristic crawl-domain
"""

import gzip
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("INDEX_WRITE_ORDER_QUERIES", "train")

import django

django.setup()

import pandas as pd
from build_corpus import OUT_DIR, QUERY_SET

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.rank import HeuristicRanker
from scripts.llm_relabel_pass3_judge import CHECKPOINT, EXTRACT_CHARS, JUDGE_PROMPT, PASS1

assert OUT_DIR.name == "train", "retrain.py reads the training index"
# The pools and their judgments are committed beside Pass 3, so the models can be rebuilt.
LABEL_DIR = Path("devdata/llm_relabel/index_write_order")
POOL_DIR = LABEL_DIR / "pools"
JUDGE_DIR = OUT_DIR / "judge"
JUDGMENTS_PATH = LABEL_DIR / "judgments.jsonl"
MODEL_DIR = OUT_DIR.parent / "models"
POOL_DEPTH = 20
QUERIES_PER_CHUNK = 6
LINE = re.compile(r"^\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*$")


def shipped_model() -> RustXGBPipeline:
    from mwmbl.search_setup import combined_ltr_model

    return combined_ltr_model


def pool(arms: list[str]) -> None:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    model = shipped_model()
    for arm in arms:
        with TinyIndex(item_factory=Document, index_path=str(OUT_DIR / f"{arm}.tinysearch")) as index:
            rankers = [
                CombinedLTRRanker(index, DummyCompleter(), model),
                HeuristicRanker(index, DummyCompleter(), float("-inf")),
            ]
            with gzip.open(POOL_DIR / f"{arm}.jsonl.gz", "wt") as out:
                for query in sorted(QUERY_SET):
                    candidates = {}
                    for ranker in rankers:
                        for doc in ranker.search(query, [], False)[:POOL_DEPTH]:
                            candidates.setdefault(
                                doc.url,
                                {
                                    "url": doc.url,
                                    "title": doc.title or "",
                                    "extract": doc.extract or "",
                                    "score": doc.score,
                                },
                            )
                    out.write(json.dumps({"query": query, "candidates": list(candidates.values())}) + "\n")
        print(arm, "pooled")


def load_pools(arms: list[str]) -> dict[str, dict[str, list[dict]]]:
    pools = {}
    for arm in arms:
        with gzip.open(POOL_DIR / f"{arm}.jsonl.gz", "rt") as file:
            pools[arm] = {record["query"]: record["candidates"] for record in map(json.loads, file)}
    return pools


def load_jsonl(path) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as file:
        return [json.loads(line) for line in file if line.strip()]


def judged() -> dict[tuple[str, str], int]:
    """(query, url) -> Haiku's overall grade: Pass 3's, then this run's."""
    grades = {}
    for record in load_jsonl(CHECKPOINT) + load_jsonl(JUDGMENTS_PATH):
        grades.setdefault((record["query"].lower().strip(), record["url"]), record["overall"])
    return grades


def batches() -> None:
    pools = load_pools(sorted(path.name.removesuffix(".jsonl.gz") for path in POOL_DIR.glob("*.jsonl.gz")))
    intents = {record["query"].lower().strip(): record["intent"] for record in load_jsonl(PASS1)}
    done = judged()
    JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    manifest, blocks, cid = [], [], 0
    for query in sorted(QUERY_SET):
        # The same URL can carry different text in the two indexes; show one, the first seen.
        candidates = {}
        for arm_pools in pools.values():
            for candidate in arm_pools.get(query, []):
                if (query, candidate["url"]) not in done:
                    candidates.setdefault(candidate["url"], candidate)
        if not candidates:
            continue
        lines = [f"\n=== QUERY: {query}\n=== INTENT: {intents.get(query, '?')}"]
        for candidate in candidates.values():
            cid += 1
            manifest.append({"id": cid, "query": query, "url": candidate["url"]})
            extract = candidate["extract"].replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{cid}. {candidate['title'] or '(no title)'} — {candidate['url']}\n   {extract}")
        blocks.append("\n".join(lines))
    (JUDGE_DIR / "manifest.jsonl").write_text("".join(json.dumps(m) + "\n" for m in manifest))
    for i in range(0, len(blocks), QUERIES_PER_CHUNK):
        (JUDGE_DIR / f"chunk_{i // QUERIES_PER_CHUNK:03d}.txt").write_text(
            JUDGE_PROMPT + "\n".join(blocks[i : i + QUERIES_PER_CHUNK])
        )
    print(f"{len(blocks)} queries, {cid} candidates, {-(-len(blocks) // QUERIES_PER_CHUNK)} chunks in {JUDGE_DIR}")


def merge() -> None:
    by_id = {m["id"]: m for m in load_jsonl(JUDGE_DIR / "manifest.jsonl")}
    done = judged()
    records, malformed = [], 0
    for path in sorted(JUDGE_DIR.glob("chunk_*.out.txt")):
        for line in path.read_text().splitlines():
            match = LINE.match(line)
            if not match:
                malformed += "||" in line
                continue
            cid, relevance, ethos, overall = map(int, match.groups())
            entry = by_id.get(cid)
            if entry is None or relevance > 3 or ethos > 3 or overall > 10:
                malformed += 1
                continue
            key = (entry["query"], entry["url"])
            if key in done:
                continue
            done[key] = overall
            records.append({**entry, "relevance": relevance, "ethos": ethos, "overall": overall})
    with open(JUDGMENTS_PATH, "a") as out:
        for record in records:
            del record["id"]
            out.write(json.dumps(record) + "\n")
    print(
        f"+{len(records)} judgments, {malformed} malformed, {len(by_id) - sum(1 for m in by_id.values() if (m['query'], m['url']) in done)} still unjudged"
    )


def pool_rows(arms: list[str], seen: set[tuple[str, str]]) -> pd.DataFrame:
    """The arms' pooled candidates as training rows, labelled like the LLM rows (overall >= 4,
    weight 1). Pairs the training frame already holds keep their row. The pools are index-only,
    as the A/B ranks, so Staan was not asked."""
    from mwmbl.rankeval.ltr.provider_features import OVERALL_THRESHOLD

    grades = judged()
    rows = {}
    for arm_pools in load_pools(arms).values():
        for query, candidates in arm_pools.items():
            for candidate in candidates:
                key = (query, candidate["url"])
                if key in seen or key in rows or key not in grades:
                    continue
                rows[key] = {
                    "query": query,
                    "url": candidate["url"],
                    "title": candidate["title"],
                    "extract": candidate["extract"],
                    "score": candidate["score"] or 0.0,
                    "label": float(grades[key] >= OVERALL_THRESHOLD),
                    "weight": 1.0,
                    "staan_asked": False,
                    "staan_rank": None,
                }
    return pd.DataFrame(list(rows.values()))


def train(name: str, arms: list[str]) -> None:
    from mwmbl.rankeval.ltr.llm_experiment import FEATURE_COLUMNS
    from mwmbl.rankeval.ltr.provider_features import NUM_ROUNDS, REG_LAMBDA, SCALE_POS_WEIGHT, training_frame

    frame = training_frame()
    seen = set(zip(frame["query"].str.lower().str.strip(), frame["url"]))
    extra = pool_rows(arms, seen)
    if len(extra):
        print(f"+{len(extra)} pool rows from {arms}, {extra['label'].mean():.1%} positive")
        frame = pd.concat([frame, extra], ignore_index=True)
    model = RustXGBPipeline(
        threshold=0.5,
        scale_pos_weight=SCALE_POS_WEIGHT,
        reg_lambda=REG_LAMBDA,
        num_rounds=NUM_ROUNDS,
        provider_features=True,
    )
    model.fit(frame[FEATURE_COLUMNS + ["staan_asked", "staan_rank"]], frame["label"], sample_weight=frame["weight"])
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / f"query-{name}.xgb"
    model.save_model(str(path))
    print(f"{len(frame)} rows -> {path}")


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    if command == "pool":
        pool(args)
    elif command == "batches":
        batches()
    elif command == "merge":
        merge()
    elif command == "train":
        train(args[0], args[1:])
