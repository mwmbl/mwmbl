"""Does Staan have a second page when its first comes back short? 20 short en-gb queries.

Fetches page 1 and page 2 (offset=10) raw, 40 paid calls, and writes
engb/staan_page2_sample.json. Needs STAAN_SEARCH_API_KEY in the environment:

    set -a && source .env && set +a
    .venv/bin/python scripts/combined_search_haiku/staan_page2.py
"""

import json
import os
import random

import requests

P = "devdata/combined_providers_eval/engb/"
URL = "https://api.staan.ai/v2/search/web"
key = os.environ["STAAN_SEARCH_API_KEY"]
rows = json.load(open(P + "rows-0.05.json"))
short = [r for r in rows if 0 < len(r["lists"]["staan"]) < 10]
random.seed(0)
sample = random.sample(short, 20)
session = requests.Session()
out = []
for r in sample:
    q = r["query"]
    rec = {"query": q, "stored": len(r["lists"]["staan"])}
    for offset in (0, 10):
        resp = session.get(
            URL,
            params={"q": q, "market": "en-gb", "count": 10, "offset": offset},
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        rec[f"status{offset}"] = resp.status_code
        if resp.ok:
            results = resp.json().get("web", {}).get("results", [])
            rec[f"n{offset}"] = len(results)
            rec[f"urls{offset}"] = [x.get("url") for x in results]
            rec[f"usable{offset}"] = sum(1 for x in results if x.get("url") and x.get("title"))
    first = set(rec.get("urls0", []))
    rec["p2_new"] = len([u for u in rec.get("urls10", []) if u not in first])
    out.append(rec)
    print(f"{rec['stored']:2d} stored | p1 {rec.get('n0')} | p2 {rec.get('n10')}, {rec['p2_new']} new | {q}")
json.dump(out, open(P + "staan_page2_sample.json", "w"), indent=1)
