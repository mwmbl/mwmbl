"""The shipped combined LTR ranking to depth 30 for the en-gb rows, with MiniLM scores."""

import json
import time
from pathlib import Path

import django

django.setup()
from django.conf import settings

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.search_setup import combined_ltr_model
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.staan import get_staan_results
from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text

rows = json.load(open("devdata/combined_providers_eval/engb/rows-0.05-with-brave.json"))
ranker = MMRRanker(CombinedLTRRanker(RemoteIndex(), DummyCompleter(), combined_ltr_model))
judge = Judge(Path(settings.SUPER_SEARCH_JUDGE_MODEL_DIR))
out = {}
same = 0
lens = []
times = []
DEPTH = 1000
for r in rows:
    q = r["query"]
    staan = get_staan_results(q)
    t0 = time.perf_counter()
    docs = ranker.search(q, staan, False)[:DEPTH]
    lens.append(len(docs))
    same += [d.url for d in docs[:10]] == r["lists"]["combined"][:10]
    t1 = time.perf_counter()
    scores = judge.score(q, [doc_text(d.title, d.extract) for d in docs]) if docs else []
    times.append((t1 - t0, time.perf_counter() - t1, len(docs)))
    out[q] = [{"url": d.url, "title": d.title, "extract": d.extract, "minilm": float(s)} for d, s in zip(docs, scores)]
json.dump(out, open("devdata/combined_providers_eval/engb/combined_deep.json", "w"))
print(
    "top10 reproduced",
    same,
    "/",
    len(rows),
    "| mean depth",
    sum(lens) / len(lens),
    "| depth>=20:",
    sum(n >= 20 for n in lens),
    "depth>=30:",
    sum(n >= 30 for n in lens),
)
import numpy as np

L = np.array(lens)
print("depth percentiles 50/90/max", np.percentile(L, 50), np.percentile(L, 90), L.max())
T = np.array(times)
print("mean LTR s %.3f, mean MiniLM s %.3f for mean %.1f docs" % (T[:, 0].mean(), T[:, 1].mean(), T[:, 2].mean()))
