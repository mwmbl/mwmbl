"""Jev's pointwise UK-relevance score for every LLM-labelled training candidate, as an LTR feature.

Scores the (query, url) pairs of the `llm`, `new` and `serving` frames of
`objective_experiment.load()`, with the Score question of
`scripts/combined_search_haiku/jev_score.py`, at most MAX_PER_REQUEST candidates a request.
The extension rows aren't scored: as with the MiniLM judges, their feature is missing and
XGBoost learns a default branch for it.

Jev has never been trained on these queries, so unlike the MiniLM judges every score is
out of sample.

Writes `devdata/combined_ltr_labels/jev_scores.json`, {"query\\turl": score}.

    JEV_API_KEY=... PYTHONPATH=. uv run python scripts/combined_ltr_labels/jev_scores.py
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from scripts.combined_ltr_labels.objective_experiment import load

sys.path.insert(0, "scripts/combined_search_haiku")
from jev_score import score_query, scores_from

OUT = Path("devdata/combined_ltr_labels/jev_scores.json")
MAX_PER_REQUEST = 100


def key(query: str, url: str) -> str:
    return f"{query}\t{url}"


def chunks() -> list[tuple[str, list[dict]]]:
    llm, _, new, serving = load()
    pairs = pd.concat([frame[["query", "url", "title", "extract"]] for frame in (llm, new, serving)])
    pairs = pairs.drop_duplicates(["query", "url"]).fillna("")
    out = []
    for query, group in pairs.groupby("query", sort=True):
        docs = group[["url", "title", "extract"]].to_dict("records")
        out += [(query, docs[i : i + MAX_PER_REQUEST]) for i in range(0, len(docs), MAX_PER_REQUEST)]
    return out


def run():
    api_key = os.environ["JEV_API_KEY"]
    work = chunks()
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda c: score_query("pointwise", c[0], c[1], api_key), work))
    scores = {}
    for (query, docs), (response, _) in zip(work, responses):
        for doc, value in zip(docs, scores_from("pointwise", response, len(docs))):
            scores[key(query, doc["url"])] = value
    OUT.write_text(json.dumps(scores))
    print(f"{len(scores)} pairs over {len(work)} requests -> {OUT}")


if __name__ == "__main__":
    run()
