"""UK-relevance and pass-3 batches for the Wikidata fill URLs, mixed with graded anchors.

Anchors are URLs both judges already graded for the query, shuffled in among the new ones
so the new grades can be checked against the old for drift.
"""

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

from mwmbl.rankeval.evaluation.wikidata_fill_report import wikidata_candidates

P = "devdata/combined_providers_eval/engb/"
S = Path(os.environ.get("HAIKU_WORK_DIR", "devdata/combined_providers_eval/haiku_work")) / "haiku_wikidata"
NB = 3
S.mkdir(parents=True, exist_ok=True)
todo = json.load(open(P + "wikidata_to_judge.json"))
text = json.load(open(P + "pool_text.json"))
docs: dict[str, dict[str, tuple[str, str]]] = {}
for name in ("combined_top30.json", "wiki_pool.json"):
    for q, ds in json.load(open(P + name)).items():
        for d in ds:
            docs.setdefault(q, {})[d["url"]] = (d["title"], d["extract"])
for q, ds in wikidata_candidates().items():
    for d in ds:
        docs.setdefault(q, {})[d["url"]] = (d["title"], d["extract"])
for q, us in text.items():
    for u, t in us.items():
        docs.setdefault(q, {}).setdefault(u, (t[0], t[1]))

uk_prompt = open("devdata/combined_providers_eval/haiku/prompts/relevance_engb.txt").read()
p3_prompt = JUDGE_PROMPT.replace(
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine.",
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine. The searcher is in the United Kingdom.",
)
assert p3_prompt != JUDGE_PROMPT
queries = sorted(todo["new"])
uk_manifest, p3_manifest = {}, {}
uk_batches, p3_batches = [[] for _ in range(NB)], [[] for _ in range(NB)]
cid = 0
for qid, q in enumerate(queries):
    urls = todo["new"][q] + todo["anchors"][q]
    random.Random(qid).shuffle(urls)
    uk_manifest[qid] = [q, urls]
    uk = [f"\n## qid {qid} | query: {q}\n"]
    p3 = [f"\n=== QUERY: {q}\n=== INTENT: (not given - infer the most likely intent)"]
    for i, u in enumerate(urls):
        t, e = docs[q][u]
        uk.append(f"[{i}] {t[:150]}\n    {u[:200]}\n    {(e or '').replace(chr(10), ' ')[:300]}\n")
        cid += 1
        p3_manifest[cid] = [q, u]
        p3.append(f"{cid}. {t or '(no title)'} — {u}\n   {(e or '').replace(chr(10), ' ')[:EXTRACT_CHARS]}")
    uk_batches[qid % NB] += uk
    p3_batches[qid % NB] += p3
json.dump({"uk": uk_manifest, "p3": p3_manifest}, open(S / "manifest.json", "w"))
for b in range(NB):
    (S / f"uk_{b}.txt").write_text(uk_prompt + "\n=== QUERIES ===\n" + "".join(uk_batches[b]))
    (S / f"p3_{b}.txt").write_text(p3_prompt + "\n".join(p3_batches[b]) + "\n")
print(len(queries), "queries", cid, "candidates", NB, "batches of each kind in", S)
