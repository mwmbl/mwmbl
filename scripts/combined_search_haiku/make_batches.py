import json
import random
import sys

S = sys.argv[1]
NB = 15
pool = json.load(open(f"{S}/pool_text.json"))
PROMPT = """You are a careful, independent search-quality judge. Each query below is followed by a numbered list of
candidate web results (title, URL, snippet), in random order. The results come from several search engines,
and you cannot tell which. Grade EACH candidate's relevance to its query, on its own merits:

  3  Excellent: directly satisfies what the searcher most likely wants (the official site for a navigational
     query; a thorough, direct answer for a question; an authoritative page on exactly that entity; the right
     local or transactional page; a current report for a news query).
  2  Good: relevant and useful, but partial, secondary, or less authoritative.
  1  Marginal: on-topic-ish but thin or tangential, the wrong sense of an ambiguous query, or an SEO/doorway page.
  0  Irrelevant: off-topic, wrong entity, spam, or broken.

Rules: judge from the title, URL and snippet only. Don't favour famous domains for their own sake, and don't
penalise a short snippet if the page is clearly the right one. Grade every candidate independently; several can
share a grade.

OUTPUT: write ONLY JSON lines, one per query, in the same order as the input, like
{"qid": 17, "grades": [3, 0, 2, 1]}
with exactly one integer grade per candidate, in candidate order.
"""
items = []
for qid, p in enumerate(pool):
    urls = sorted(p["docs"])
    random.Random(qid).shuffle(urls)
    items.append((qid, p["query"], urls, p["docs"]))
manifest = {qid: urls for qid, _, urls, _ in items}
json.dump(manifest, open(f"{S}/haiku/manifest.json", "w"))
for b in range(NB):
    parts = [PROMPT, "\n=== QUERIES ===\n"]
    for qid, q, urls, docs in items[b::NB]:
        parts.append(f"\n## qid {qid} | query: {q}\n")
        for i, u in enumerate(urls):
            t, e, _ = docs[u]
            parts.append(f"[{i}] {t[:150]}\n    {u[:200]}\n    {(e or '').replace(chr(10), ' ')[:300]}\n")
    open(f"{S}/haiku/batch_{b:02d}.txt", "w").write("".join(parts))
print(NB, "batches", sum(len(u) for u in manifest.values()), "urls")
