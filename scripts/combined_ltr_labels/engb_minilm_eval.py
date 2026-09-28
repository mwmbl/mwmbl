"""End-to-end en-gb evaluation of `ndcg+new` with the fine-tuned MiniLM judges as features.

`minilm_experiment.py` cross-validated the judges as features. This serves them: every arm
is trained on all 849 queries plus the serving-pool labels (its `all` setting), and ranks the
same fresh retrieval through `CombinedLTRRanker` + MMR as `engb_eval.py` does. At serving
each candidate the ranker scores is scored by each judge on its title and extract, and the
scores are appended to mwmbl_rank's features in `minilm_scores.MINILM_MODELS` order.

`shipped` and `ndcg+new` rank again here: arms are only comparable within one retrieval.

    rank         rank the pool with every arm -> engb_minilm_arms.json
    batches      UK pass-3 batches for top-ten URLs no judgment covers, plus anchors
    consolidate  judge output -> pass3_engb_minilm.jsonl
    report       NDCG@10 of every arm against the union of all en-gb pass-3 judgments

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys

import django
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
from django.conf import settings

from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text
from scripts.combined_ltr_labels.engb_eval import (
    LABELS,
    SHIPPED,
    BoosterModel,
    Run,
    batches,
    consolidate,
    rank_all,
    report,
)
from scripts.combined_ltr_labels.minilm_experiment import FEATURE_SETS, SCORES, minilm_columns
from scripts.combined_ltr_labels.minilm_scores import MINILM_MODELS, MODELS
from scripts.combined_ltr_labels.objective_experiment import features, load, train

MINILM_RUN = Run(
    arms=LABELS / "engb_minilm_arms.json",
    judgments=LABELS / "pass3_engb_minilm.jsonl",
    work=LABELS / "engb_minilm_work",
    baselines=(SHIPPED, "ndcg+new"),
)
OBJECTIVE = "ndcg"
ARMS = {"ndcg+new": "base", "ndcg+new+pointwise": "pointwise", "ndcg+new+both": "both", "ndcg+new+all3": "all3"}


class JudgeScores:
    """Each judge's score of each (query, url), computed once and shared by every arm."""

    def __init__(self):
        self.judges = {model: Judge(MODELS / model / "onnx") for model in MINILM_MODELS}
        self.cache: dict[str, dict[tuple[str, str], float]] = {model: {} for model in MINILM_MODELS}

    def columns(self, records: list[dict], models: list[str]) -> np.ndarray:
        for model in models:
            cache = self.cache[model]
            unscored = [record for record in records if (record["query"], record["url"]) not in cache]
            if unscored:
                texts = [doc_text(record["title"], record["extract"]) for record in unscored]
                values = self.judges[model].score(unscored[0]["query"], texts)
                cache.update({(record["query"], record["url"]): value for record, value in zip(unscored, values)})
        return np.array(
            [[self.cache[model][(record["query"], record["url"])] for model in models] for record in records],
            dtype=np.float32,
        ).reshape(len(records), len(models))


class MinilmBoosterModel(BoosterModel):
    def __init__(self, booster, models: list[str], scores: JudgeScores):
        super().__init__(booster)
        self.models = models
        self.scores = scores

    def features(self, records: list[dict]) -> np.ndarray:
        return np.concatenate([super().features(records), self.scores.columns(records, self.models)], axis=1)


def rank():
    scores = json.loads(SCORES.read_text())
    assert list(scores) == MINILM_MODELS
    llm, ext, new, _ = load()
    frames = {"llm": llm, "ext": ext, "new": new}
    frame = pd.concat(frames.values(), ignore_index=True)
    base_feats = np.concatenate([features(f) for f in frames.values()])
    # The extension rows' judge columns stay NaN: XGBoost learns a default branch for them.
    judge_feats = np.concatenate([minilm_columns(f, scores) for f in frames.values()])

    judge_scores = JudgeScores()
    models = {SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))}
    for arm, feature_set in ARMS.items():
        judges = FEATURE_SETS[feature_set]
        columns = [MINILM_MODELS.index(model) for model in judges]
        booster = train(OBJECTIVE, frame, np.concatenate([base_feats, judge_feats[:, columns]], axis=1))
        models[arm] = MinilmBoosterModel(booster, judges, judge_scores)
        print("trained", arm, flush=True)
    rank_all(models, MINILM_RUN.arms)


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "rank":
        rank()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](MINILM_RUN)
