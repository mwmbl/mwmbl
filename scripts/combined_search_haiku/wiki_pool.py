"""Wikipedia candidates for the en-gb rows, from the en-gb external cache, scored by MiniLM."""

import json
from pathlib import Path

import django

django.setup()
from django.conf import settings

from mwmbl.tinysearchengine.rank import get_wiki_results
from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text

rows = json.load(open("devdata/combined_providers_eval/engb/rows-0.05-with-brave.json"))
judge = Judge(Path(settings.SUPER_SEARCH_JUDGE_MODEL_DIR))
out = {}
for r in rows:
    q = r["query"]
    docs = get_wiki_results(q)
    scores = judge.score(q, [doc_text(d.title, d.extract) for d in docs]) if docs else []
    out[q] = [{"url": d.url, "title": d.title, "extract": d.extract, "minilm": float(s)} for d, s in zip(docs, scores)]
json.dump(out, open("devdata/combined_providers_eval/engb/wiki_pool.json", "w"))
n = [len(v) for v in out.values()]
print("queries", len(out), "wiki docs", sum(n), "queries with none", sum(x == 0 for x in n))
