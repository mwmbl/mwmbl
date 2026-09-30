"""Jev (TypeSafe) orderings of the ndcg+new candidates on en-gb, against the holistic reference.

The candidates are ndcg+new's top 30 without MMR (`jev_pool.py`), which is what the reference
`staan-first, fill ndcg+new, no MMR` fills from, so every arm orders the same pool. Jev scores
each candidate with the pointwise UK-relevance Score of
`scripts/combined_search_haiku/jev_score.py`, one request per query. The arms:

    staan-first, fill Jev    Staan in Staan's order, then the rest by Jev
    Jev + Staan rank         Jev's score minus STAAN_WEIGHT x Staan's position (10 if absent)
    Jev re-rank              Jev's score alone

    score   Jev scores for every candidate -> jev_engb_scores.jsonl
    arms    the reference and the Jev arms -> engb_jev_arms.json
    batches / consolidate / report   pass-3 NDCG, as engb_eval.py

The holistic comparison is `holistic_eval.py batches jev`.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mwmbl.rankeval.evaluation.haiku_arms_report import staan_first
from scripts.combined_ltr_labels.engb_eval import ENGB, Run, batches, consolidate, report
from scripts.combined_ltr_labels.jev_pool import POOL

sys.path.insert(0, "scripts/combined_search_haiku")
from jev_score import score_query, scores_from

LABELS = Path("devdata/combined_ltr_labels")
SCORES = LABELS / "jev_engb_scores.jsonl"
POOL_ARM = "ndcg+new, no MMR"
REFERENCE = "staan-first, fill ndcg+new, no MMR"
FILL_JEV = "staan-first, fill Jev"
BLEND_JEV = "Jev + Staan rank"
PURE_JEV = "Jev re-rank"
STAAN_WEIGHT = 0.05
NUM_RESULTS = 10
JEV_RUN = Run(
    arms=LABELS / "engb_jev_arms.json",
    judgments=LABELS / "pass3_engb_jev.jsonl",
    work=LABELS / "engb_jev_work",
    baselines=(REFERENCE, "brave"),
)


def candidates() -> dict[str, list[dict]]:
    pool = json.loads(POOL.read_text())
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    text = json.loads((ENGB / "pool_text.json").read_text())
    out = {}
    for query, entry in pool.items():
        pages = {**text[query], **entry["pages"]}
        urls = dict.fromkeys(rows[query]["lists"]["staan"][:NUM_RESULTS] + entry["lists"][POOL_ARM])
        out[query] = [{"url": url, "title": pages[url][0], "extract": pages[url][1]} for url in urls]
    return out


def score():
    api_key = os.environ["JEV_API_KEY"]
    docs = candidates()
    queries = sorted(docs)
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda q: score_query("pointwise", q, docs[q], api_key), queries))
    with open(SCORES, "w") as f:
        for query, (response, _) in zip(queries, responses):
            for doc, value in zip(docs[query], scores_from("pointwise", response, len(docs[query]))):
                f.write(json.dumps({"query": query, "url": doc["url"], "score": value}) + "\n")
    print(f"{sum(map(len, docs.values()))} candidates over {len(queries)} queries -> {SCORES}")


def arms():
    pool = json.loads(POOL.read_text())
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    text = json.loads((ENGB / "pool_text.json").read_text())
    jev: dict[str, dict[str, float]] = {}
    for line in open(SCORES):
        record = json.loads(line)
        jev.setdefault(record["query"], {})[record["url"]] = record["score"]
    saved = json.loads((LABELS / "engb_staan_arms.json").read_text())

    out, same_reference = {}, 0
    for query, entry in pool.items():
        staan = rows[query]["lists"]["staan"][:NUM_RESULTS]
        ranked = entry["lists"][POOL_ARM]
        scores = jev[query]
        position = {url: i for i, url in enumerate(staan)}
        everything = list(dict.fromkeys(staan + ranked))
        lists = {
            REFERENCE: staan_first(staan, ranked),
            FILL_JEV: staan_first(staan, sorted(ranked, key=lambda u: -scores[u])),
            BLEND_JEV: sorted(everything, key=lambda u: -(scores[u] - STAAN_WEIGHT * position.get(u, NUM_RESULTS)))[
                :NUM_RESULTS
            ],
            PURE_JEV: sorted(everything, key=lambda u: -scores[u])[:NUM_RESULTS],
        }
        same_reference += lists[REFERENCE] == saved[query]["lists"][REFERENCE]
        shown = {url for urls in lists.values() for url in urls}
        pages = {**text[query], **entry["pages"]}
        out[query] = {"lists": lists, "pages": {url: pages[url] for url in shown}}
    JEV_RUN.arms.write_text(json.dumps(out))
    print(f"{len(out)} queries -> {JEV_RUN.arms}; reference matches engb_staan_arms.json on {same_reference}")


if __name__ == "__main__":
    command = sys.argv[1]
    commands = {"score": score, "arms": arms}
    if command in commands:
        commands[command]()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](JEV_RUN)
