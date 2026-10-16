"""Helper: prepare one Pass-3 judging wave.

Dumps the next N un-judged queries into ONE shared manifest (--tag) and writes
per-chunk prompt files (JUDGE_PROMPT + chunk's query blocks) for parallel
subagents. Each subagent reads a chunk file and writes its score lines to a
unique output file; all are merged back against the single shared manifest.

Usage::
    DJANGO_SETTINGS_MODULE=... uv run python scripts/_p3_make_wave.py \
        --tag w1 --n-queries 32 --chunk-queries 4 --outdir /path/to/scratch
"""

import json
import os
from argparse import ArgumentParser

from llm_relabel_pass3_judge import (
    EXTRACT_CHARS,
    JUDGE_PROMPT,
    MANIFEST,
    _done_pairs,
    _intents,
    _pool,
)


def main():
    p = ArgumentParser()
    p.add_argument("--tag", required=True)
    p.add_argument("--n-queries", type=int, required=True)
    p.add_argument("--chunk-queries", type=int, default=4)
    p.add_argument("--outdir", required=True)
    a = p.parse_args()

    pool, intents, done = _pool(), _intents(), _done_pairs()
    todo = [q for q, r in pool.items() if any((q, c["url"]) not in done for c in r["candidates"])]
    batch = todo[: a.n_queries]

    manifest, blocks, cid = [], [], 0
    for q in batch:
        lines = [f"\n=== QUERY: {q}\n=== INTENT: {intents.get(q, '?')}"]
        for c in pool[q]["candidates"]:
            if (q, c["url"]) in done:
                continue
            cid += 1
            manifest.append({"id": cid, "query": q, "url": c["url"]})
            title = c["title"] or "(no title)"
            extract = (c["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{cid}. {title} — {c['url']}\n   {extract}")
        blocks.append("\n".join(lines))

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST.format(tag=a.tag), "w") as f:
        for m in manifest:
            f.write(json.dumps(m) + "\n")

    os.makedirs(a.outdir, exist_ok=True)
    n = 0
    for i in range(0, len(blocks), a.chunk_queries):
        chunk = blocks[i : i + a.chunk_queries]
        path = f"{a.outdir}/p3_{a.tag}_chunk{i // a.chunk_queries}.txt"
        with open(path, "w") as f:
            f.write(JUDGE_PROMPT + "\n".join(chunk))
        cand = sum(b.count("\n   ") for b in chunk)
        print(f"{path}  ({len(chunk)} queries, ~{cand} candidates)")
        n += 1
    print(f"tag={a.tag}: {len(batch)} queries, {cid} candidates, {n} chunks")


if __name__ == "__main__":
    main()
