import json

import django

django.setup()
import numpy as np

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.search_setup import combined_ltr_model
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.staan import get_staan_results

rows = json.load(open("devdata/combined_providers_eval/engb/rows-0.05-with-brave.json"))[:60]
inner = CombinedLTRRanker(RemoteIndex(), DummyCompleter(), combined_ltr_model)
stats = []
orig = inner.order_results


def counting(terms, results, is_complete):
    out = orig(terms, results, is_complete)
    stats[-1].update(
        candidates=len(results),
        kept=len(out),
        staan=sum(1 for d in results if getattr(d, "source", None) is not None and d.source.name == "STAAN"),
    )
    return out


inner.order_results = counting
ranker = MMRRanker(inner)
for r in rows:
    stats.append({"q": r["query"]})
    final = ranker.search(r["query"], get_staan_results(r["query"]), False)
    stats[-1]["final"] = len(final)
for k in ("candidates", "staan", "kept", "final"):
    v = np.array([s.get(k, 0) for s in stats])
    print(f"{k:10s} median {np.median(v):6.1f}  mean {v.mean():6.1f}")
for s in stats[:12]:
    print(s)
