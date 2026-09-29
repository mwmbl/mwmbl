"""End-to-end en-gb evaluation of the MiniLM cascade, alone and filling the slots after Staan.

`minilm_cascade_experiment.py` found that the judge scoring only the stage-1 top 20 keeps
the whole cross-validated gain of scoring every candidate. This serves that cascade:

1. `ndcg+new` ranks every candidate, as `engb_eval.BoosterModel`.
2. The judge (`minilm-both-v1`) scores the top 20 it keeps, and a model trained with the
   judge present only on its training rows' out-of-fold stage-1 top 20 re-orders them.

Arms, on one fresh retrieval with Staan's results at Staan's real ranks:

- `shipped`, `ndcg+new`, and `ndcg+new` with Staan-monotone constraints (`MONOTONE`).
- `cascade top 20`, unconstrained and monotone.
- `staan-first, fill cascade top 20`: Staan's results in Staan's order, then the cascade's.
- `staan-first, fill MiniLM(ndcg+new top 30)`: the fill ordered by the judge alone, as the
  best arm of `combined-search-haiku-eval.md` does.

The report also scores, from `devdata/combined_providers_eval/`, the arms of that doc's
earlier retrieval: Staan, Staan-first filled by the shipped ranker, and Staan-first filled by
MiniLM over the LTR's top 30 plus Wikipedia (0.841 there). Their URLs are all judged.

    rank         rank the pool with every arm -> engb_cascade_arms.json
    batches      UK pass-3 batches for top-ten URLs no judgment covers, plus anchors
    consolidate  judge output -> pass3_engb_cascade.jsonl
    report       NDCG@10 of every arm against the union of all en-gb pass-3 judgments

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import xgboost as xgb
from django.conf import settings

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.haiku_arms_report import engb_arms, staan_first
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from scripts.combined_ltr_labels.engb_eval import (
    ENGB,
    LABELS,
    SHIPPED,
    BoosterModel,
    Run,
    batches,
    consolidate,
    report,
    staan_documents,
)
from scripts.combined_ltr_labels.engb_minilm_eval import JudgeScores
from scripts.combined_ltr_labels.minilm_cascade_experiment import CONSTRAINTS, TEACHER, Training

CASCADE_RUN = Run(
    arms=LABELS / "engb_cascade_arms.json",
    judgments=LABELS / "pass3_engb_cascade.jsonl",
    work=LABELS / "engb_cascade_work",
    baselines=("ndcg+new", "staan-first, fill MiniLM(ndcg+new top 30)"),
)
DEPTH = 20
FILL_DEPTH = 30
FOLDS = 5
MONOTONE = "staan"
NUM_RESULTS = 10


class CascadeModel:
    """Stage 1 orders every candidate; the judge scores its top `DEPTH`, which stage 2 re-orders.

    Returns what `LTRRanker` expects: 0 for a candidate the filter drops, stage 1's sigmoid
    (below 1) for the rest, and 1 + stage 2's sigmoid for the top `DEPTH`, which puts them first.
    """

    def __init__(self, stage1: BoosterModel, stage2: xgb.Booster, scores: JudgeScores):
        self.stage1 = stage1
        self.stage2 = stage2
        self.scores = scores

    def predict(self, records: list[dict]) -> np.ndarray:
        stage1_scores = self.stage1.predict(records)
        order = np.argsort(-stage1_scores, kind="stable")
        top = order[stage1_scores[order] > 0][:DEPTH]
        judge = np.full(len(records), np.nan, dtype=np.float32)
        judge[top] = self.scores.columns([records[i] for i in top], [TEACHER])[:, 0]
        feats = np.concatenate([self.stage1.features(records), judge[:, None]], axis=1)
        margins = self.stage2.predict(xgb.DMatrix(feats), output_margin=True)
        final = stage1_scores.copy()
        final[top] = 1 + 1 / (1 + np.exp(-margins[top]))
        return final


def train_models(constraints: str, scores: JudgeScores) -> tuple[BoosterModel, CascadeModel]:
    """Stage 1 and the cascade, trained on all 849 queries plus the serving-pool labels."""
    data = Training(CONSTRAINTS[constraints])
    everything = data.masks(set())
    queries = np.array(sorted(data.frames["llm"]["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    out_of_fold = data.out_of_fold([set(fold) for fold in np.array_split(queries, FOLDS)])
    stage1 = BoosterModel(data.fit(everything, data.base))
    stage2 = data.fit(everything, data.masked(out_of_fold, DEPTH))
    print(f"trained {constraints}", flush=True)
    return stage1, CascadeModel(stage1, stage2, scores)


def rank():
    scores = JudgeScores()
    stage1, cascade = train_models("none", scores)
    monotone_stage1, monotone_cascade = train_models(MONOTONE, scores)
    models = {
        SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH)),
        "ndcg+new": stage1,
        f"ndcg+new, {MONOTONE} monotone": monotone_stage1,
        f"cascade top {DEPTH}": cascade,
        f"cascade top {DEPTH}, {MONOTONE} monotone": monotone_cascade,
    }
    index = RemoteIndex()
    rankers = {arm: MMRRanker(CombinedLTRRanker(index, DummyCompleter(), model)) for arm, model in models.items()}
    rows = json.loads((ENGB / "rows-0.05.json").read_text())
    text = json.loads((ENGB / "pool_text.json").read_text())
    earlier = json.loads((ENGB / "combined_top30.json").read_text())
    wiki = json.loads((ENGB / "wiki_pool.json").read_text())
    out = {}
    for i, row in enumerate(rows):
        query = row["query"]
        staan = row["lists"]["staan"][:NUM_RESULTS]
        retrieval = rankers[SHIPPED].retrieve(query)
        ranked = {
            arm: ranker.search_retrieved(retrieval, staan_documents(row, text[query]))
            for arm, ranker in rankers.items()
        }
        pages = {page.url: [page.title, page.extract] for results in ranked.values() for page in results}
        lists = {arm: [page.url for page in results[:NUM_RESULTS]] for arm, results in ranked.items()}

        lists[f"staan-first, fill cascade top {DEPTH}"] = staan_first(
            staan, [page.url for page in ranked[f"cascade top {DEPTH}"]]
        )
        fill = ranked["ndcg+new"][:FILL_DEPTH]
        judge = scores.columns(
            [{"query": query, "url": p.url, "title": p.title, "extract": p.extract} for p in fill], [TEACHER]
        )
        by_judge = [page.url for _, page in sorted(zip(-judge[:, 0], fill), key=lambda pair: pair[0])]
        lists[f"staan-first, fill MiniLM(ndcg+new top {FILL_DEPTH})"] = staan_first(staan, by_judge)

        for arm, urls in engb_arms(row, earlier, wiki).items():
            if arm in ("staan", "staan-first + fill", "staan-first, fill MiniLM(LTR top 30 + Wikipedia)"):
                lists[f"earlier: {arm}"] = urls
        for doc in earlier[query] + wiki[query]:
            pages.setdefault(doc["url"], [doc["title"], doc["extract"]])
        for url, (title, extract, _source) in text[query].items():
            pages.setdefault(url, [title, extract])
        shown = {url for urls in lists.values() for url in urls}
        out[query] = {"lists": lists, "pages": {url: page for url, page in pages.items() if url in shown}}
        if i % 25 == 0:
            print(i, query, flush=True)
    CASCADE_RUN.arms.write_text(json.dumps(out))


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "rank":
        rank()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](CASCADE_RUN)
