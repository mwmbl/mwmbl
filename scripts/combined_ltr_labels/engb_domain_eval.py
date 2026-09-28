"""End-to-end en-gb evaluation of `ndcg+new` with domain-quality features, alone and beside
the served MiniLM judge (`both`).

`domain_experiment.py` cross-validated the domain features. Here every arm is trained on all
849 queries plus the serving-pool labels and ranks the same fresh retrieval through
`CombinedLTRRanker` + MMR, as `engb_minilm_eval.py` does. The en-gb queries are not in the
SERP table at all, so no count can see a query's own SERP. The host-quality model is fitted
on every labelled row, and cross-fitted for the training rows.

    rank         rank the pool with every arm -> engb_domain_arms.json
    batches      UK pass-3 batches for top-ten URLs no judgment covers, plus anchors
    consolidate  judge output -> pass3_engb_domain.jsonl
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
from scripts.combined_ltr_labels.domain_experiment import (
    STATIC_COLUMNS,
    Learned,
    hq_inputs,
    learned_columns,
    with_ethos,
)
from scripts.combined_ltr_labels.domain_features import apex_of, domain_columns, host_of
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
from scripts.combined_ltr_labels.engb_minilm_eval import JudgeScores
from scripts.combined_ltr_labels.minilm_experiment import SCORES, minilm_columns
from scripts.combined_ltr_labels.objective_experiment import features, load, train

DOMAIN_RUN = Run(
    arms=LABELS / "engb_domain_arms.json",
    judgments=LABELS / "pass3_engb_domain.jsonl",
    work=LABELS / "engb_domain_work",
    baselines=("ndcg+new", "ndcg+new+both"),
)
OBJECTIVE = "ndcg"
BOTH = "minilm-both-v1"
ARMS = {
    "ndcg+new": [],
    "ndcg+new+domain": ["raw+crawl"],
    "ndcg+new+hq": ["hq"],
    "ndcg+new+both": ["minilm"],
    "ndcg+new+both+domain": ["minilm", "raw+crawl"],
}


class DomainBoosterModel(BoosterModel):
    def __init__(self, booster, groups: list[str], learned: Learned, judge_scores: JudgeScores):
        super().__init__(booster)
        self.groups = groups
        self.learned = learned
        self.judge_scores = judge_scores

    def features(self, records: list[dict]) -> np.ndarray:
        base = super().features(records)
        frame = pd.DataFrame({"query": [r["query"] for r in records], "url": [r["url"] for r in records]})
        frame["host"] = frame["url"].map(host_of)
        frame["apex"] = frame["host"].map(apex_of)
        static = domain_columns(frame)
        parts = [base]
        for group in self.groups:
            if group in STATIC_COLUMNS:
                parts.append(static[:, STATIC_COLUMNS[group]])
            elif group == "minilm":
                parts.append(self.judge_scores.columns(records, [BOTH]))
            else:
                parts.append(self.learned.columns(frame, hq_inputs(static, base))[group])
        return np.concatenate(parts, axis=1)


def rank():
    llm, ext, new, _ = load()
    new = with_ethos(llm, new)
    frames = {"llm": llm, "ext": ext, "new": new}
    for frame in frames.values():
        frame["host"] = frame["url"].map(host_of)
        frame["apex"] = frame["host"].map(apex_of)
    base = {name: features(frame) for name, frame in frames.items()}
    static = {name: domain_columns(frame) for name, frame in frames.items()}
    hq_in = {name: hq_inputs(static[name], base[name]) for name in frames}
    scores = json.loads(SCORES.read_text())
    both = list(scores).index(BOTH)
    judge = {name: minilm_columns(frame, scores)[:, [both]] for name, frame in frames.items()}

    labelled = pd.concat([llm, new], ignore_index=True)
    fitted, (ext_learned,) = learned_columns(
        labelled, np.concatenate([hq_in["llm"], hq_in["new"]]), [(ext, hq_in["ext"])]
    )
    whole = Learned(labelled, np.concatenate([hq_in["llm"], hq_in["new"]]))
    num_llm = len(llm)
    train_frame = pd.concat([llm, ext, new], ignore_index=True)
    columns = {
        "raw+crawl": np.concatenate([static["llm"], static["ext"], static["new"]]),
        "minilm": np.concatenate([judge["llm"], judge["ext"], judge["new"]]),
        **{
            name: np.concatenate([fitted[name][:num_llm], ext_learned[name], fitted[name][num_llm:]]) for name in fitted
        },
    }
    base_train = np.concatenate([base["llm"], base["ext"], base["new"]])

    judge_scores = JudgeScores()
    models = {SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))}
    for arm, groups in ARMS.items():
        booster = train(OBJECTIVE, train_frame, np.concatenate([base_train] + [columns[g] for g in groups], axis=1))
        models[arm] = DomainBoosterModel(booster, groups, whole, judge_scores)
        print("trained", arm, flush=True)
    rank_all(models, DOMAIN_RUN.arms)


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "rank":
        rank()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](DOMAIN_RUN)
