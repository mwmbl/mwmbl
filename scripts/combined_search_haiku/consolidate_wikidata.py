"""Parse the Haiku outputs for the Wikidata batches into the committed judgment files.

Writes haiku/relevance_engb_wikidata.jsonl and haiku/pass3_engb_wikidata.jsonl. Each record
has `anchor`: true for a URL that was already graded in relevance_engb / pass3_engb, and
re-graded here only to check the new run against the old.
"""

import json
import os
import re
from pathlib import Path

E = Path("devdata/combined_providers_eval")
S = Path(os.environ.get("HAIKU_WORK_DIR", E / "haiku_work")) / "haiku_wikidata"
manifest = json.load(open(S / "manifest.json"))
todo = json.load(open(E / "engb/wikidata_to_judge.json"))
anchors = {(q, u) for q, us in todo["anchors"].items() for u in us}

uk = []
for path in sorted(S.glob("uk_*.out.jsonl")):
    for line in path.read_text().splitlines():
        if line.strip().startswith("{"):
            rec = json.loads(line)
            q, urls = manifest["uk"][str(rec["qid"])]
            assert len(rec["grades"]) == len(urls), rec["qid"]
            for i, g in rec["grades"].items():
                uk.append(
                    {"query": q, "url": urls[int(i)], "relevance": int(g), "anchor": (q, urls[int(i)]) in anchors}
                )
p3 = []
for path in sorted(S.glob("p3_*.out.txt")):
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(\d+)\s*\|\|\s*(\d)\s*\|\|\s*(\d)\s*\|\|\s*(\d+)", line)
        if m:
            q, u = manifest["p3"][m.group(1)]
            p3.append(
                {
                    "query": q,
                    "url": u,
                    "relevance": int(m.group(2)),
                    "ethos": int(m.group(3)),
                    "overall": int(m.group(4)),
                    "anchor": (q, u) in anchors,
                }
            )
assert len(p3) == len(manifest["p3"]) and len(uk) == len(manifest["p3"])
for name, records in (("relevance_engb_wikidata", uk), ("pass3_engb_wikidata", p3)):
    records.sort(key=lambda r: (r["query"], r["url"]))
    (E / f"haiku/{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    print(name, len(records))
