"""Jev's score as a feature of the rank:ndcg+new Combined Search model, cross-validated.

`jev_scores.py` scores every LLM-labelled candidate. Jev has never seen these queries, so all
849 are cross-validated, in `objective_experiment.py`'s folds. Arms:

- `base`: ndcg+new, as `objective_experiment.py`.
- `jev, all candidates`: the same with `jev` as one more feature on every candidate. The
  extension rows are unscored, so their `jev` is missing and XGBoost learns a default branch.
- `jev, top 30`: serving's cascade. `base` ranks every candidate, Jev scores its top 30 and the
  `all candidates` model re-orders them; the rest keep `base`'s order below them.
- `jev alone` and `jev + staan rank`: the hand-made orderings of `jev_experiment.py`, for
  reference (Jev's score, and Jev's score minus STAAN_WEIGHT x Staan's position).

Writes `devdata/combined_ltr_labels/jev_feature_experiment.json` with per-query scores.

    PYTHONPATH=. uv run python scripts/combined_ltr_labels/jev_feature_experiment.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from scripts.combined_ltr_labels.jev_scores import OUT as SCORES
from scripts.combined_ltr_labels.jev_scores import key
from scripts.combined_ltr_labels.minilm_cascade_experiment import TOP_K_OFFSET, Fixed, top_k
from scripts.combined_ltr_labels.objective_experiment import FOLDS, features, kept, load, score_population, train

LABELS = Path("devdata/combined_ltr_labels")
OUT = LABELS / "jev_feature_experiment.json"
OBJECTIVE = "ndcg"
DEPTH = 30
STAAN_WEIGHT = 0.05
NOT_IN_STAAN = 10


def jev_column(frame: pd.DataFrame, scores: dict[str, float]) -> np.ndarray:
    return np.array([scores.get(key(q, u), np.nan) for q, u in zip(frame["query"], frame["url"])], dtype=np.float32)


def with_jev(base: np.ndarray, jev: np.ndarray) -> np.ndarray:
    return np.concatenate([base, jev[:, None]], axis=1)


def staan_blend(frame: pd.DataFrame, jev: np.ndarray) -> np.ndarray:
    position = frame["staan_rank"].astype(float).fillna(NOT_IN_STAAN).to_numpy()
    return jev - STAAN_WEIGHT * position


def run():
    scores = json.loads(SCORES.read_text())
    llm, ext, new, serving = load()
    frames = {"llm": llm, "ext": ext, "new": new, "serving": serving}
    base = {name: features(frame) for name, frame in frames.items()}
    jev = {name: jev_column(frame, scores) for name, frame in frames.items()}
    for name in ("llm", "new", "serving"):
        assert not np.isnan(jev[name]).any(), f"unscored {name} pairs"
    full = {name: with_jev(base[name], jev[name]) for name in frames}

    queries = np.array(sorted(llm["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    folds = np.array_split(queries, FOLDS)
    arms = ["base", "jev, all candidates", f"jev, top {DEPTH}", "jev alone", "jev + staan rank"]
    results = {arm: {"original": {}, "serving": {}} for arm in arms}

    for fold, test_queries in enumerate(folds):
        test = set(test_queries)
        test_norm = {q.lower().strip() for q in test}
        masks = {
            "llm": ~llm["query"].isin(test).to_numpy(),
            "ext": ~ext["query"].str.lower().str.strip().isin(test_norm).to_numpy(),
            "new": ~new["query"].isin(test).to_numpy(),
        }
        frame = pd.concat([frames[name][mask] for name, mask in masks.items()], ignore_index=True)
        stage1 = train(OBJECTIVE, frame, np.concatenate([base[name][mask] for name, mask in masks.items()]))
        model = train(OBJECTIVE, frame, np.concatenate([full[name][mask] for name, mask in masks.items()]))

        for population, name in [("original", "llm"), ("serving", "serving")]:
            rows = frames[name]["query"].isin(test).to_numpy()
            test_frame = frames[name][rows].reset_index(drop=True)
            feats = full[name][rows]
            stage1_scores = stage1.predict(xgb.DMatrix(base[name][rows]))
            model_scores = model.predict(xgb.DMatrix(feats))
            keep = kept(test_frame, feats)
            present = top_k(test_frame["query"].to_numpy(), stage1_scores, keep, DEPTH)
            cascade = np.where(present, TOP_K_OFFSET + model_scores, stage1_scores)
            by_arm = {
                "base": stage1_scores,
                "jev, all candidates": model_scores,
                f"jev, top {DEPTH}": cascade,
                "jev alone": jev[name][rows],
                "jev + staan rank": staan_blend(test_frame, jev[name][rows]),
            }
            for arm, arm_scores in by_arm.items():
                results[arm][population].update(score_population(Fixed(arm_scores), test_frame, feats))
        print(f"fold {fold} done", flush=True)

    OUT.write_text(json.dumps(results))
    report(results)


def report(results: dict[str, dict[str, dict]] | None = None):
    results = results or json.loads(OUT.read_text())
    rng = np.random.default_rng(0)
    for population in ("serving", "original"):
        print(f"\n## {population} pool, 5-fold CV over all 849 queries\n")
        print("| Arm | NDCG@10 | vs base, 95% CI | weak in top 10 | index in top 10 |")
        print("|---|---|---|---|---|")
        queries = sorted(q for q, v in results["base"][population].items() if not np.isnan(v["ndcg@10"]))
        baseline = np.array([results["base"][population][q]["ndcg@10"] for q in queries])
        for arm, by_query in results.items():
            values = np.array([by_query[population][q]["ndcg@10"] for q in queries])
            diff = values - baseline
            means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
            weak = np.nanmean([by_query[population][q]["weak_in_top10"] for q in queries])
            index = np.nanmean([by_query[population][q]["index_in_top10"] for q in queries])
            print(
                f"| {arm} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
                f"{np.percentile(means, 97.5):+.4f}] | {weak:.1%} | {index:.1%} |"
            )


if __name__ == "__main__":
    run()
