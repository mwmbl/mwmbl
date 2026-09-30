"""UK-relevance and pass-3 batches for the URLs the Jev arms rank that no judgment covers.

Reads engb/jev_ungraded.json (``jev_arms_report --dump-ungraded``). Two anchors per query,
URLs both judges already graded, are shuffled in so the new grades can be checked for drift.
"""

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

from mwmbl.rankeval.evaluation.haiku_arms_report import load_judgments

P = Path("devdata/combined_providers_eval")
S = Path(os.environ.get("HAIKU_WORK_DIR", P / "haiku_work")) / "haiku_jev"
NB = 6
ANCHORS = 2
S.mkdir(parents=True, exist_ok=True)
ungraded = json.load(open(P / "engb/jev_ungraded.json"))
candidates = json.load(open(P / "engb/jev_candidates.json"))
relevance, pass3 = load_judgments("haiku/relevance_engb.jsonl"), load_judgments("haiku/pass3_engb.jsonl")

uk_prompt = open(P / "haiku/prompts/relevance_engb.txt").read()
p3_prompt = JUDGE_PROMPT.replace(
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine.",
    "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine. The searcher is in the United Kingdom.",
)
assert p3_prompt != JUDGE_PROMPT
uk_manifest, p3_manifest, anchors = {}, {}, []
uk_batches, p3_batches = [[] for _ in range(NB)], [[] for _ in range(NB)]
cid = 0
for qid, q in enumerate(sorted(ungraded)):
    docs = {d["url"]: d for d in candidates[q]}
    both = sorted(set(docs) & set(relevance.get(q, {})) & set(pass3.get(q, {})))
    rng = random.Random(qid)
    anchor_urls = rng.sample(both, min(ANCHORS, len(both)))
    anchors += [[q, u] for u in anchor_urls]
    urls = ungraded[q] + anchor_urls
    rng.shuffle(urls)
    uk_manifest[qid] = [q, urls]
    uk = [f"\n## qid {qid} | query: {q}\n"]
    p3 = [f"\n=== QUERY: {q}\n=== INTENT: (not given - infer the most likely intent)"]
    for i, u in enumerate(urls):
        t, e = docs[u]["title"], docs[u]["extract"]
        uk.append(f"[{i}] {t[:150]}\n    {u[:200]}\n    {(e or '').replace(chr(10), ' ')[:300]}\n")
        cid += 1
        p3_manifest[cid] = [q, u]
        p3.append(f"{cid}. {t or '(no title)'} — {u}\n   {(e or '').replace(chr(10), ' ')[:EXTRACT_CHARS]}")
    uk_batches[qid % NB] += uk
    p3_batches[qid % NB] += p3
json.dump({"uk": uk_manifest, "p3": p3_manifest, "anchors": anchors}, open(S / "manifest.json", "w"))
for b in range(NB):
    (S / f"uk_{b}.txt").write_text(uk_prompt + "\n=== QUERIES ===\n" + "".join(uk_batches[b]))
    (S / f"p3_{b}.txt").write_text(p3_prompt + "\n".join(p3_batches[b]) + "\n")
print(len(ungraded), "queries", cid, "candidates", NB, "batches of each kind in", S)
