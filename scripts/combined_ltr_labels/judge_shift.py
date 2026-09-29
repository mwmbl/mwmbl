"""Does the en-gb eval's judging prompt grade index pages lower, relative to Staan's, than the
training labels' prompt does?

The LTR's index pages grade 4.5 in cross-validation but 3.1 on en-gb, even off .uk hosts.
The two sets were judged by Haiku with different prompts: the training labels with the
query's Pass-1 intent, the en-gb eval with "The searcher is in the United Kingdom" and no
intent. This re-grades already-judged training pairs, Staan's and the index's side by side,
with the en-gb prompt. Within one judge, the index drift minus the Staan drift is the shift.

    batches  -> judge_shift_work/batch_NN.txt
    report   compare out_NN.txt with the original grades
"""

import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS

from scripts.combined_ltr_labels.engb_eval import LINE, uk_prompt

LABELS = Path("devdata/combined_ltr_labels")
WORK = LABELS / "judge_shift_work"
ORIGINAL = Path("devdata/llm_relabel/pass3_judgments.jsonl")
SERVING_POOL_JUDGMENTS = LABELS / "pass3_serving_pool.jsonl"
QUERIES = 80
PER_SOURCE = 5
QUERIES_PER_BATCH = 20


def grades() -> dict[tuple[str, str], int]:
    out = {(j["query"], j["url"]): j["overall"] for j in map(json.loads, open(ORIGINAL))}
    for j in map(json.loads, open(SERVING_POOL_JUDGMENTS)):
        if not j.get("anchor"):
            out.setdefault((j["query"], j["url"]), j["overall"])
    return out


def batches():
    pool = json.loads((LABELS / "pool.json").read_text())
    known = grades()
    rng = random.Random(0)
    queries = rng.sample(sorted(pool), QUERIES)
    manifest, blocks, next_id = {}, [], 1
    for query in queries:
        rows = [row for row in pool[query] if (query, row["url"]) in known]
        staan = [row for row in rows if row["source"] != "index"]
        index = [row for row in rows if row["source"] == "index"]
        chosen = rng.sample(staan, min(PER_SOURCE, len(staan))) + rng.sample(index, min(PER_SOURCE, len(index)))
        rng.shuffle(chosen)
        lines = [f"\n=== QUERY: {query}\n=== INTENT: (not given - infer the most likely intent)"]
        for row in chosen:
            manifest[next_id] = [query, row["url"], row["source"] != "index"]
            extract = (row["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{next_id}. {row['title'] or '(no title)'} — {row['url']}\n   {extract}")
            next_id += 1
        blocks.append("\n".join(lines))
    WORK.mkdir(parents=True, exist_ok=True)
    prompt = uk_prompt()
    for b in range(0, len(blocks), QUERIES_PER_BATCH):
        (WORK / f"batch_{b // QUERIES_PER_BATCH:02d}.txt").write_text(
            prompt + "\n".join(blocks[b : b + QUERIES_PER_BATCH]) + "\n"
        )
    (WORK / "manifest.json").write_text(json.dumps(manifest))
    print(f"{len(blocks)} queries, {len(manifest)} candidates")


def report():
    manifest = {int(k): v for k, v in json.loads((WORK / "manifest.json").read_text()).items()}
    known = grades()
    rows = []
    for path in sorted(WORK.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            match = LINE.match(line)
            if match:
                cid, _relevance, _ethos, overall = map(int, match.groups())
                query, url, staan = manifest[cid]
                rows.append((path.stem, staan, overall - known[(query, url)], known[(query, url)], overall))
    print(f"{len(rows)} of {len(manifest)} graded")
    for judge in sorted({r[0] for r in rows}) + ["all"]:
        mine = [r for r in rows if judge in ("all", r[0])]
        staan = np.array([r[2] for r in mine if r[1]])
        index = np.array([r[2] for r in mine if not r[1]])
        print(
            f"{judge}: staan drift {staan.mean():+.2f} (n {len(staan)}), index drift {index.mean():+.2f} "
            f"(n {len(index)}), index - staan {index.mean() - staan.mean():+.2f}"
        )
    staan = np.array([r[2] for r in rows if r[1]])
    index = np.array([r[2] for r in rows if not r[1]])
    rng = np.random.default_rng(0)
    diffs = [rng.choice(index, len(index)).mean() - rng.choice(staan, len(staan)).mean() for _ in range(2000)]
    print(
        f"index - staan shift {index.mean() - staan.mean():+.2f} [{np.percentile(diffs, 2.5):+.2f}, "
        f"{np.percentile(diffs, 97.5):+.2f}]"
    )
    for label, staan_flag in (("staan", True), ("index", False)):
        before = np.array([r[3] for r in rows if r[1] == staan_flag])
        after = np.array([r[4] for r in rows if r[1] == staan_flag])
        print(f"{label}: original mean {before.mean():.2f}, en-gb prompt mean {after.mean():.2f}")


if __name__ == "__main__":
    {"batches": batches, "report": report}[sys.argv[1]]()
