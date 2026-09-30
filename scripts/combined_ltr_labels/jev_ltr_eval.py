"""End to end on en-gb: ndcg+new with Jev as a feature, served as a top-30 cascade.

Both models train on all 849 queries, as `jev_feature_experiment.py`'s `base` and
`jev, all candidates`, which none of the en-gb queries is among. The cascade is what serving
would run, inside Combined Search's own `CombinedLTRRanker`:

1. ndcg+new scores every retrieved candidate.
2. Jev scores the top 30 the majority-terms filter keeps, in one request.
3. The Jev model re-orders those 30 above everything else, which keeps its stage-1 order.

Jev scores come from `jev_engb_scores.jsonl` where the pool already has them, and from a live
Jev call otherwise. Writes `engb_jev_ltr_arms.json`:

    staan-first, fill ndcg+new, no MMR   the holistic reference
    ndcg+new+jev, no MMR                 the cascade
    staan-first, fill ndcg+new+jev, no MMR
    Jev + Staan rank                     jev_experiment.py's arm, on this retrieval
    Jev + Staan rank, w=0.15             the same with the weight tuned on the training queries

    rank     rank the en-gb queries to depth 30 -> engb_jev_ltr_ranked.json, then `arms`
    arms     the depth-30 lists -> the arms above -> engb_jev_ltr_arms.json
    batches / consolidate / report   pass-3 NDCG, as engb_eval.py

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import pandas as pd
import xgboost as xgb
from django.conf import settings

import mwmbl_rank
from mwmbl.rankeval.evaluation.haiku_arms_report import staan_first
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from scripts.combined_ltr_labels.engb_eval import (
    ENGB,
    SHIPPED,
    BoosterModel,
    Run,
    batches,
    consolidate,
    rank_all,
    report,
)
from scripts.combined_ltr_labels.engb_staan_experiment import load_engb
from scripts.combined_ltr_labels.jev_experiment import BLEND_JEV, NUM_RESULTS, REFERENCE, STAAN_WEIGHT
from scripts.combined_ltr_labels.jev_experiment import SCORES as ENGB_SCORES
from scripts.combined_ltr_labels.jev_feature_experiment import DEPTH, OBJECTIVE, jev_column, with_jev
from scripts.combined_ltr_labels.jev_scores import OUT as TRAINING_SCORES
from scripts.combined_ltr_labels.jev_scores import key
from scripts.combined_ltr_labels.objective_experiment import MATCH_TERMS, NUM_TERMS, features, train

sys.path.insert(0, "scripts/combined_search_haiku")
from jev_score import score_query, scores_from

LABELS = Path("devdata/combined_ltr_labels")
CASCADE = "ndcg+new+jev"
STAGE1 = "ndcg+new"
TOP_OFFSET = 2.0
RANKED = LABELS / "engb_jev_ltr_ranked.json"
# The Staan weight that maximises Jev + Staan rank's pass-3 NDCG@10 on the 849 training
# queries' serving pool (0.885, against 0.875 at jev_experiment.py's 0.05, which was picked on
# these en-gb queries themselves).
TUNED_STAAN_WEIGHT = 0.15
TUNED_BLEND = f"Jev + Staan rank, w={TUNED_STAAN_WEIGHT}"
JEV_LTR_RUN = Run(
    arms=LABELS / "engb_jev_ltr_arms.json",
    judgments=LABELS / "pass3_engb_jev_ltr.jsonl",
    work=LABELS / "engb_jev_ltr_work",
    baselines=(REFERENCE, BLEND_JEV, "brave"),
)


class JevScores:
    """Jev's score for a (query, url): from the en-gb pool's scores, else a live call."""

    def __init__(self):
        self.scores: dict[str, float] = {}
        for line in open(ENGB_SCORES):
            record = json.loads(line)
            self.scores[key(record["query"], record["url"])] = record["score"]
        self.live = 0

    def get(self, records: list[dict]) -> np.ndarray:
        missing = [r for r in records if key(r["query"], r["url"]) not in self.scores]
        if missing:
            docs = [{"url": r["url"], "title": r["title"], "extract": r["extract"]} for r in missing]
            response, _ = score_query("pointwise", missing[0]["query"], docs, os.environ["JEV_API_KEY"])
            for doc, value in zip(docs, scores_from("pointwise", response, len(docs))):
                self.scores[key(missing[0]["query"], doc["url"])] = value
            self.live += len(missing)
        return np.array([self.scores[key(r["query"], r["url"])] for r in records], dtype=np.float32)


class JevCascade:
    """Stage 1 orders every candidate; the Jev model re-orders stage 1's kept top DEPTH above the rest."""

    def __init__(self, stage1: xgb.Booster, model: xgb.Booster, jev: JevScores):
        self.stage1, self.model, self.jev = stage1, model, jev

    def predict(self, records: list[dict]) -> np.ndarray:
        feats = np.array(mwmbl_rank.RustXGBPipeline.extract_features(records, True), dtype=np.float32)
        from_staan = np.array([record["from_staan"] for record in records])
        keep = from_staan | (feats[:, MATCH_TERMS] > feats[:, NUM_TERMS] / 2)
        stage1 = self.stage1.predict(xgb.DMatrix(feats), output_margin=True)
        top = np.flatnonzero(keep)[np.argsort(-stage1[keep], kind="stable")][:DEPTH]
        scores = 1 / (1 + np.exp(-stage1))
        if len(top):
            jev = self.jev.get([records[i] for i in top])
            margins = self.model.predict(xgb.DMatrix(with_jev(feats[top], jev)), output_margin=True)
            scores[top] = TOP_OFFSET + 1 / (1 + np.exp(-margins))
        return np.where(keep, scores, 0.0)


def rank():
    llm, ext, new, *_ = load_engb()
    frames = [llm, ext, new]
    frame = pd.concat(frames, ignore_index=True)
    base = [features(f) for f in frames]
    training_scores = json.loads(TRAINING_SCORES.read_text())
    stage1 = train(OBJECTIVE, frame, np.concatenate(base))
    model = train(
        OBJECTIVE, frame, np.concatenate([with_jev(b, jev_column(f, training_scores)) for b, f in zip(base, frames)])
    )
    jev = JevScores()
    models = {
        SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH)),
        STAGE1: BoosterModel(stage1),
        CASCADE: JevCascade(stage1, model, jev),
    }
    rank_all(models, RANKED, keep=DEPTH, no_mmr=(STAGE1, CASCADE))
    print(f"{jev.live} candidates scored live")
    arms()


def blend(pool: list[str], scores: dict[str, float], position: dict[str, int], weight: float) -> list[str]:
    """Jev's score minus `weight` x Staan's position (NUM_RESULTS where Staan didn't return it)."""
    return sorted(pool, key=lambda u: -(scores[u] - weight * position.get(u, NUM_RESULTS)))[:NUM_RESULTS]


def arms():
    """The ranked depth-30 lists -> the arms, each cut to ten."""
    jev = JevScores()
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    text = json.loads((ENGB / "pool_text.json").read_text())
    ranked_arms = json.loads(RANKED.read_text())
    for query, entry in ranked_arms.items():
        staan = rows[query]["lists"]["staan"][:NUM_RESULTS]
        ranked = entry["lists"][f"{STAGE1}, no MMR"]
        position = {url: i for i, url in enumerate(staan)}
        pool = list(dict.fromkeys(staan + ranked))
        # Staan results the ranker's top 30 left out carry Staan's own text.
        pages = {**text[query], **entry["pages"]}
        pool_records = [{"query": query, "url": url, "title": pages[url][0], "extract": pages[url][1]} for url in pool]
        scores = dict(zip(pool, jev.get(pool_records)))
        cascade = entry["lists"][f"{CASCADE}, no MMR"]
        entry["lists"] = {
            REFERENCE: staan_first(staan, ranked),
            f"{CASCADE}, no MMR": cascade[:NUM_RESULTS],
            f"staan-first, fill {CASCADE}, no MMR": staan_first(staan, cascade),
            BLEND_JEV: blend(pool, scores, position, STAAN_WEIGHT),
            TUNED_BLEND: blend(pool, scores, position, TUNED_STAAN_WEIGHT),
        }
        shown = {url for urls in entry["lists"].values() for url in urls}
        entry["pages"] = {url: pages[url][:2] for url in shown}
    JEV_LTR_RUN.arms.write_text(json.dumps(ranked_arms))
    print(f"{len(ranked_arms)} queries -> {JEV_LTR_RUN.arms}; {jev.live} candidates scored live")


if __name__ == "__main__":
    command = sys.argv[1]
    commands = {"rank": rank, "arms": arms}
    if command in commands:
        commands[command]()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](JEV_LTR_RUN)
