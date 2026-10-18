"""Blind, shuffled Haiku batches for a rows file, with a UK searcher in the prompt."""

import json
import random
import sys
from pathlib import Path

rows_path, text_path, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
NB = 15
N = 10
out_dir.mkdir(parents=True, exist_ok=True)
rows = json.load(open(rows_path))
text = json.load(open(text_path))
PROMPT = """You are a careful, independent search-quality judge. The searcher is in the United Kingdom.
Each query below is followed by a numbered list of candidate web results (title, URL, snippet), in random
order. The results come from several search engines, and you cannot tell which. Grade EACH candidate's
relevance to its query, for a UK searcher, on its own merits:

  3  Excellent: directly satisfies what the searcher most likely wants (the official site for a navigational
     query; a thorough, direct answer for a question; an authoritative page on exactly that entity; the right
     local or transactional page; a current report for a news query).
  2  Good: relevant and useful, but partial, secondary, or less authoritative.
  1  Marginal: on-topic-ish but thin or tangential, the wrong sense of an ambiguous query, or an SEO/doorway page.
  0  Irrelevant: off-topic, wrong entity, spam, or broken.

Location: where the right answer depends on the country (government services, local businesses and places,
weather, jobs, shops and prices, laws, sport, TV), a result that serves a different country - for example a
US state DMV page for "renew driving licence" - is the wrong sense of the query: grade it at most 1. Where the
answer is the same everywhere (a definition, a film, a song, a global brand's official site), location doesn't
matter.

Rules: judge from the title, URL and snippet only. Don't favour famous domains for their own sake, and don't
penalise a short snippet if the page is clearly the right one. Grade every candidate independently; several can
share a grade.

OUTPUT: write ONLY JSON lines, one per query, in the same order as the input. Key each grade by its candidate
number, and include EVERY candidate number shown for that query, for example
{"qid": 17, "grades": {"0": 3, "1": 0, "2": 2, "3": 1}}
Before writing each line, check that the number of keys equals the number of candidates listed for that query.
"""
manifest = {}
items = []
missing = 0
for qid, r in enumerate(rows):
    docs = text.get(r["query"], {})
    need = {u for a in ("staan", "combined", "brave") for u in r["lists"][a][:N]}
    missing += len(need - set(docs))
    urls = sorted(need & set(docs))
    random.Random(qid).shuffle(urls)
    manifest[qid] = urls
    items.append((qid, r["query"], urls, docs))
json.dump(manifest, open(out_dir / "manifest.json", "w"))
for b in range(NB):
    parts = [PROMPT, "\n=== QUERIES ===\n"]
    for qid, q, urls, docs in items[b::NB]:
        parts.append(f"\n## qid {qid} | query: {q}\n")
        for i, u in enumerate(urls):
            t, e, _ = docs[u]
            parts.append(f"[{i}] {t[:150]}\n    {u[:200]}\n    {(e or '').replace(chr(10), ' ')[:300]}\n")
    open(out_dir / f"batch_{b:02d}.txt", "w").write("".join(parts))
print(NB, "batches", sum(len(u) for u in manifest.values()), "urls, missing text", missing)
