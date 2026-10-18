"""Rebuild title/extract for every URL in the eval rows' arms, from caches only (no Staan key set)."""

import json
import os
import sys

os.environ["STAAN_SEARCH_API_KEY"] = ""
from mwmbl.rankeval.evaluation import compare_combined_providers as ccp
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.search_setup import combined_ltr_model
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.staan import get_staan_results

rows = json.load(open("devdata/combined_providers_eval/rows-0.05.json"))
ranker = MMRRanker(CombinedLTRRanker(RemoteIndex(), DummyCompleter(), combined_ltr_model))
out = []
miss_staan = 0
brave_miss = 0
comb_match = 0
for i, r in enumerate(rows):
    q = r["query"]
    text = {}
    staan = get_staan_results(q)
    if r["lists"]["staan"] and not staan:
        miss_staan += 1
    for d in staan:
        text.setdefault(d.url, (d.title, d.extract, "staan"))
    comb = ranker.search(q, staan, False)
    if [d.url for d in comb[:10]] == r["lists"]["combined"][:10]:
        comb_match += 1
    for d in comb:
        text.setdefault(d.url, (d.title, d.extract, "index"))
    if ccp.brave_results.check_call_in_cache(q):
        for d in ccp.brave_results(q):
            text.setdefault(d["url"], (d["title"], d["extract"], "brave"))
    else:
        brave_miss += 1
    need = set(u for a in ("staan", "combined", "brave") for u in r["lists"][a][:10])
    missing = need - set(text)
    out.append({"query": q, "docs": {u: text[u] for u in need if u in text}, "missing": sorted(missing)})
    print(i, q, len(need), "missing", len(missing), file=sys.stderr)
json.dump(out, open(sys.argv[1], "w"))
print(
    "staan cache misses",
    miss_staan,
    "brave cache misses",
    brave_miss,
    "combined reproduced",
    comb_match,
    "/",
    len(rows),
)
print("total urls", sum(len(o["docs"]) for o in out), "missing", sum(len(o["missing"]) for o in out))
