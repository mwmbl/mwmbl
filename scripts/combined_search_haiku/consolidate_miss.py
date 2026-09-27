"""Parse the Haiku outputs for the index-text batches into haiku/relevance_engb_indextext.jsonl.

Each record has `anchor`: true for a URL shown with the text it was first graded on, so
the new run can be checked against relevance_engb.jsonl for drift; false for a URL shown
with the title and extract the index stored for it.
"""

import json
import os
from pathlib import Path

E = Path("devdata/combined_providers_eval")
S = Path(os.environ.get("HAIKU_WORK_DIR", E / "haiku_work")) / "haiku_miss"
manifest = json.loads((S / "manifest.json").read_text())

records = []
skipped = 0
for path in sorted(S.glob("uk_*.out.jsonl")):
    for line in path.read_text().splitlines():
        if line.strip().startswith("{"):
            rec = json.loads(line)
            query, candidates = manifest[str(rec["qid"])]
            # A judge that miscounts a query's candidates can't be lined up with them.
            if len(rec["grades"]) != len(candidates):
                print("skipping miscounted qid", rec["qid"], query)
                skipped += len(candidates)
                continue
            for i, grade in rec["grades"].items():
                url, anchor = candidates[int(i)]
                records.append({"query": query, "url": url, "relevance": int(grade), "anchor": anchor})
assert len(records) + skipped == sum(len(c) for _, c in manifest.values()), (len(records), "graded")
records.sort(key=lambda r: (r["query"], r["url"]))
out = E / "haiku/relevance_engb_indextext.jsonl"
out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
print(out, len(records))
