"""Pass-3 judge batches for the unjudged pairs of `pool.json`, plus anchors.

The prompt is Pass 3's own (`scripts/llm_relabel_pass3_judge.py`, relevance / ethos /
overall), with each query's Pass-1 intent, so the new grades sit on the same scale as the
43k in `pass3_judgments.jsonl` they are trained alongside. Candidates are shuffled within a
query and carry no provenance.

Almost every unjudged pair is an index result, so a batch of them alone would show the judge
a pool of mostly weak pages, which can shift where it puts its grades. Each query therefore
also carries ANCHORS already-judged pairs from its pool, preferring Staan's, re-graded blind
beside the new ones: `consolidate.py` compares them with their original grades to measure
drift.

Writes `$WORK/batch_NN.txt` and `$WORK/manifest.json` (id -> [query, url, is_anchor]).
"""

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

POOL = Path("devdata/combined_ltr_labels/pool.json")
INTENTS = Path("devdata/llm_relabel/pass1_intents.jsonl")
WORK = Path(os.environ.get("HAIKU_WORK_DIR", "devdata/combined_ltr_labels/haiku_work"))
ANCHORS = 2
CANDIDATES_PER_BATCH = 250


def run():
    pool = json.loads(POOL.read_text())
    intents = {record["query"]: record["intent"] for record in map(json.loads, open(INTENTS))}

    blocks = []
    manifest = {}
    next_id = 1
    for i, (query, rows) in enumerate(sorted(pool.items())):
        rng = random.Random(i)
        new = [row for row in rows if not row["judged"]]
        if not new:
            continue
        judged = [row for row in rows if row["judged"]]
        rng.shuffle(judged)
        judged.sort(key=lambda row: row["source"] == "index")
        chosen = [(row, False) for row in new] + [(row, True) for row in judged[:ANCHORS]]
        rng.shuffle(chosen)

        lines = [f"\n=== QUERY: {query}\n=== INTENT: {intents[query]}"]
        for row, is_anchor in chosen:
            manifest[next_id] = [query, row["url"], is_anchor]
            extract = (row["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{next_id}. {row['title'] or '(no title)'} — {row['url']}\n   {extract}")
            next_id += 1
        blocks.append((len(chosen), "\n".join(lines)))

    WORK.mkdir(parents=True, exist_ok=True)
    batches: list[list[str]] = [[]]
    size = 0
    for count, block in blocks:
        if size + count > CANDIDATES_PER_BATCH and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(block)
        size += count
    for b, batch in enumerate(batches):
        (WORK / f"batch_{b:02d}.txt").write_text(JUDGE_PROMPT + "\n".join(batch) + "\n")
    (WORK / "manifest.json").write_text(json.dumps(manifest))
    anchors = sum(is_anchor for _, _, is_anchor in manifest.values())
    print(f"{len(blocks)} queries, {len(manifest)} candidates ({anchors} anchors) in {len(batches)} batches")


if __name__ == "__main__":
    run()
