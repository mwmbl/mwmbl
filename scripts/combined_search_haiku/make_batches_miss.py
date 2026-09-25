"""UK-relevance batches for the index's own title and extract of the Brave URLs it holds.

The Brave text of each URL was already graded (haiku/relevance_engb.jsonl). Grading the
text the index stored for the same URL shows whether the index represents the page worse
than Brave's snippet does: an extraction problem rather than a recall or ranking one.

Two anchors per query - other URLs of that query, shown with the text they were first
graded on - are mixed in to check the new run against the old for drift. A query never
shows the same URL twice, so the judge cannot line up the two versions of a page.
"""

import json
import os
import random
from pathlib import Path

E = Path("devdata/combined_providers_eval")
S = Path(os.environ.get("HAIKU_WORK_DIR", E / "haiku_work")) / "haiku_miss"
NUM_BATCHES = 4
ANCHORS_PER_QUERY = 2
S.mkdir(parents=True, exist_ok=True)

probe = json.loads((E / "engb/miss_probe.json").read_text())
pool_text = json.loads((E / "engb/pool_text.json").read_text())
graded = {}
for line in (E / "haiku/relevance_engb.jsonl").read_text().splitlines():
    record = json.loads(line)
    graded.setdefault(record["query"], {})[record["url"]] = record["relevance"]

new: dict[str, dict[str, tuple[str, str]]] = {}
for record in probe:
    if record["set"] is not None and record["stored"] is not None:
        stored = record["stored"]
        new.setdefault(record["query"], {})[record["url"]] = (stored["title"] or "", stored["extract"] or "")

prompt = (E / "haiku/prompts/relevance_engb.txt").read_text()
manifest = {}
batches = [[] for _ in range(NUM_BATCHES)]
for qid, query in enumerate(sorted(new)):
    rng = random.Random(qid)
    anchor_urls = sorted(u for u in graded[query] if u not in new[query] and u in pool_text[query])
    anchors = rng.sample(anchor_urls, min(ANCHORS_PER_QUERY, len(anchor_urls)))
    candidates = [(u, *new[query][u], False) for u in new[query]]
    candidates += [(u, *pool_text[query][u][:2], True) for u in anchors]
    rng.shuffle(candidates)
    manifest[qid] = [query, [[url, anchor] for url, _, _, anchor in candidates]]
    lines = [f"\n## qid {qid} | query: {query}\n"]
    for i, (url, title, extract, _) in enumerate(candidates):
        lines.append(
            f"[{i}] {(title or '')[:150]}\n    {url[:200]}\n    {(extract or '').replace(chr(10), ' ')[:300]}\n"
        )
    batches[qid % NUM_BATCHES] += lines

(S / "manifest.json").write_text(json.dumps(manifest))
for b, lines in enumerate(batches):
    (S / f"uk_{b}.txt").write_text(prompt + "\n=== QUERIES ===\n" + "".join(lines))
print(len(manifest), "queries,", sum(len(v[1]) for v in manifest.values()), "candidates in", S)
