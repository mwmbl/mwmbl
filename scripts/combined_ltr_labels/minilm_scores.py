"""Scores every LLM-dataset row and serving-pool pair with the three fine-tuned MiniLM judges.

The judges (`devdata/judge_train/models/minilm-{both,pointwise,pairs}-v1`, gitignored) share
the frozen splits of `devdata/judge_train/eval_manifest.json`, so all three saw the same
340 LLM training queries and 85 validation queries. `minilm_experiment.py` handles that.

Writes `devdata/combined_ltr_labels/minilm_scores.json`: model -> "query\\turl" -> score.
Extension-dataset rows aren't scored: at 76 pairs a second on CPU they would take hours.
"""

import json
from pathlib import Path

import pandas as pd

from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text

LLM_DATASET = Path("devdata/rankeval-2026-04/learning-to-rank-llm.csv.gz")
POOL = Path("devdata/combined_ltr_labels/pool.json")
MODELS = Path("devdata/judge_train/models")
MINILM_MODELS = ["minilm-both-v1", "minilm-pointwise-v1", "minilm-pairs-v1"]
OUT = Path("devdata/combined_ltr_labels/minilm_scores.json")


def pairs() -> dict[str, list[tuple[str, str]]]:
    """query -> [(url, doc text)], over the LLM rows and the serving pool."""
    llm = pd.read_csv(LLM_DATASET, lineterminator="\n")
    docs: dict[tuple[str, str], str] = {}
    for query, url, title, extract in zip(llm["query"], llm["url"], llm["title"], llm["extract"]):
        docs[(query, url)] = doc_text(
            title if isinstance(title, str) else "", extract if isinstance(extract, str) else ""
        )
    for query, rows in json.loads(POOL.read_text()).items():
        for row in rows:
            docs.setdefault((query, row["url"]), doc_text(row["title"], row["extract"]))
    by_query: dict[str, list[tuple[str, str]]] = {}
    for (query, url), text in docs.items():
        by_query.setdefault(query, []).append((url, text))
    return by_query


def run():
    by_query = pairs()
    print(f"{sum(len(v) for v in by_query.values())} pairs over {len(by_query)} queries", flush=True)
    scores = json.loads(OUT.read_text()) if OUT.exists() else {}
    for model in MINILM_MODELS:
        if model in scores:
            continue
        judge = Judge(MODELS / model / "onnx")
        model_scores = {}
        for i, (query, docs) in enumerate(by_query.items()):
            values = judge.score(query, [text for _, text in docs])
            model_scores.update({f"{query}\t{url}": value for (url, _), value in zip(docs, values)})
            if i % 100 == 0:
                print(f"{model} {i}/{len(by_query)}", flush=True)
        scores[model] = model_scores
        OUT.write_text(json.dumps(scores))


if __name__ == "__main__":
    run()
