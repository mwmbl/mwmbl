"""Pass-3 (relevance / ethos / overall) batches for every graded URL in the en-gb pool."""

import json
import os
import random
import sys

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

S = os.environ.get("HAIKU_WORK_DIR", "devdata/combined_providers_eval/haiku_work")
P = "devdata/combined_providers_eval/engb/"
NB = 20
rows = json.load(open(P + "rows-0.05-with-brave.json"))
deep = json.load(open(P + "combined_top30.json"))
wiki = json.load(open(P + "wiki_pool.json"))
text = json.load(open(P + "pool_text.json"))
urls = {}
m = json.load(open(f"{S}/haiku_uk/manifest.json"))
for qid, us in m.items():
    urls.setdefault(rows[int(qid)]["query"], set()).update(us)
dm = json.load(open(f"{S}/haiku_uk_deep/manifest.json"))
dq = json.load(open(f"{S}/haiku_uk_deep/queries.json"))
for qid, us in dm.items():
    urls.setdefault(dq[qid], set()).update(us)


def doc(q, u):
    for d in deep.get(q, []) + wiki.get(q, []):
        if d["url"] == u:
            return d["title"], d["extract"]
    t = text[q][u]
    return t[0], t[1]


prompt = JUDGE_PROMPT.replace(
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine.",
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine. The searcher is in the United Kingdom.",
)
assert prompt != JUDGE_PROMPT
queries = sorted(urls)
manifest = {}
cid = 0
batches = [[] for _ in range(NB)]
for i, q in enumerate(queries):
    us = sorted(urls[q])
    random.Random(i).shuffle(us)
    lines = [f"\n=== QUERY: {q}\n=== INTENT: (not given - infer the most likely intent)"]
    for u in us:
        cid += 1
        manifest[cid] = [q, u]
        t, e = doc(q, u)
        lines.append(f"{cid}. {t or '(no title)'} — {u}\n   {(e or '').replace(chr(10), ' ')[:EXTRACT_CHARS]}")
    batches[i % NB] += lines
json.dump(manifest, open(f"{S}/haiku_p3/manifest.json", "w"))
for b, lines in enumerate(batches):
    open(f"{S}/haiku_p3/batch_{b:02d}.txt", "w").write(prompt + "\n".join(lines) + "\n")
print(len(queries), "queries", cid, "candidates in", NB, "batches")
