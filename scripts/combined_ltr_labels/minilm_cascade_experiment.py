"""Does the MiniLM feature's gain survive a cascade, where the judge scores only the top K?

The judge costs about 17 ms a candidate. Scoring all ~150 candidates of a query takes 2.5 s,
while the LTR's top 30 takes about 0.5 s. So serving would run two stages:

1. `ndcg+new` without MiniLM ranks every candidate.
2. The judge scores that ranking's top K, and a second model with `minilm-both-v1` as a
   feature re-orders those K. Everything below K keeps its stage-1 order.

This differs from the MiniLM re-rank in `combined-search-haiku-eval.md`, which ordered the
top K by the judge's score alone and lost. Here the judge is one feature beside the others.

Arms, in `minilm_experiment.py`'s `all` setting over its 5 folds of the 424 judge-eval queries:

- `base`: `ndcg+new`, no MiniLM.
- `all candidates`: MiniLM on every candidate. This reproduces `all/both`.
- `top K, masked`: stage 2 trained with MiniLM present only on its training rows' stage-1
  top K, as it would see it at serving. The training rows' stage-1 ranking is out of fold,
  so it isn't flattered by having trained on those rows.
- `top K, full`: the `all candidates` model re-orders the stage-1 top K.

Each run can constrain every model to be monotone in Staan's features (`CONSTRAINTS`):
`staan-rank` only lets the score fall as Staan's rank grows, and `staan` also only lets it
rise with `in_staan`. Monotonicity holds with the other features fixed, so it doesn't force
Staan's order onto results that differ in anything else; `staan_inversions` measures how
often that order is still broken in the top ten.

Writes `devdata/combined_ltr_labels/minilm_cascade_experiment[_<constraints>].json` with
per-query scores.

    PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_cascade_experiment.py [staan-rank|staan]
    PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_cascade_experiment.py compare staan-rank
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from scripts.combined_ltr_labels.minilm_experiment import minilm_columns, normalize
from scripts.combined_ltr_labels.objective_experiment import (
    FEATURE_NAMES,
    features,
    kept,
    load,
    score_population,
    train,
)

LABELS = Path("devdata/combined_ltr_labels")
SCORES = LABELS / "minilm_scores.json"
MANIFEST = Path("devdata/judge_train/eval_manifest.json")

TEACHER = "minilm-both-v1"
FOLDS = 5
OBJECTIVE = "ndcg"
DEPTHS = [10, 20, 30]
# Puts every re-ordered top-K candidate above every candidate below K.
TOP_K_OFFSET = 1e6
CONSTRAINTS = {
    "none": {},
    "staan-rank": {FEATURE_NAMES.index("staan_rank"): -1},
    "staan": {FEATURE_NAMES.index("staan_rank"): -1, FEATURE_NAMES.index("in_staan"): 1},
}


def out_path(constraints: str) -> Path:
    suffix = "" if constraints == "none" else f"_{constraints}"
    return LABELS / f"minilm_cascade_experiment{suffix}.json"


class Fixed:
    """Stands in for a booster whose scores are already known, for `score_population`."""

    def __init__(self, scores: np.ndarray):
        self.scores = scores

    def predict(self, dmatrix) -> np.ndarray:
        return self.scores


def top_k(queries: np.ndarray, scores: np.ndarray, keep: np.ndarray, k: int) -> np.ndarray:
    """Whether each candidate is among its query's k best-scored candidates the filter keeps."""
    ranks = pd.Series(np.where(keep, scores, -np.inf)).groupby(queries).rank(method="first", ascending=False)
    return keep & (ranks.to_numpy() <= k)


def evaluate(frame: pd.DataFrame, feats: np.ndarray, scores: np.ndarray) -> dict[str, dict]:
    """`score_population`'s metrics, plus the share of the top ten's Staan pairs out of Staan's order."""
    result = score_population(Fixed(scores), frame, feats)
    keep = kept(frame, feats)
    staan_ranks = frame["staan_rank"].astype(float).to_numpy()
    for query, index in frame.groupby("query").indices.items():
        top = index[keep[index]][np.argsort(-scores[index][keep[index]], kind="stable")][:10]
        ranks = staan_ranks[top][~np.isnan(staan_ranks[top])]
        pairs = len(ranks) * (len(ranks) - 1) / 2
        inverted = sum(int(np.sum(ranks[i + 1 :] < ranks[i])) for i in range(len(ranks)))
        result[query]["staan_inversions"] = inverted / pairs if pairs else float("nan")
    return result


def with_judge(base: np.ndarray, judge: np.ndarray, present: np.ndarray | None = None) -> np.ndarray:
    column = judge if present is None else np.where(present, judge, np.nan)
    return np.concatenate([base, column[:, None]], axis=1)


class Training:
    """The training frames with their LTR and judge features, and the cascade's pieces."""

    def __init__(self, monotone: dict[int, int]):
        judge_scores = {TEACHER: json.loads(SCORES.read_text())[TEACHER]}
        self.monotone = monotone
        llm, ext, new, serving = load()
        self.frames = {"llm": llm, "ext": ext, "new": new, "serving": serving}
        self.base = {name: features(frame) for name, frame in self.frames.items()}
        self.judge = {name: minilm_columns(frame, judge_scores)[:, 0] for name, frame in self.frames.items()}
        self.keep = {name: kept(self.frames[name], self.base[name]) for name in ("llm", "serving")}
        self.queries = {name: frame["query"].to_numpy() for name, frame in self.frames.items()}
        self.full = {name: with_judge(self.base[name], self.judge[name]) for name in self.frames}
        serving_position = {key: i for i, key in enumerate(serving["query"] + "\t" + serving["url"])}
        self.new_rows = np.array([serving_position[key] for key in new["query"] + "\t" + new["url"]])

    def masks(self, test: set[str]) -> dict[str, np.ndarray]:
        """The training rows whose query isn't in `test`."""
        test_norm = {normalize(q) for q in test}
        return {
            "llm": ~self.frames["llm"]["query"].isin(test).to_numpy(),
            "ext": ~self.frames["ext"]["query"].map(normalize).isin(test_norm).to_numpy(),
            "new": ~self.frames["new"]["query"].isin(test).to_numpy(),
        }

    def fit(self, masks: dict[str, np.ndarray], feats: dict[str, np.ndarray]) -> xgb.Booster:
        frame = pd.concat([self.frames[name][masks[name]] for name in masks], ignore_index=True)
        stacked = np.concatenate([feats[name][masks[name]] for name in masks])
        return train(OBJECTIVE, frame, stacked, self.monotone)

    def out_of_fold(self, query_folds: list[set[str]]) -> dict[str, np.ndarray]:
        """Stage-1 scores for every LLM and serving row from a model that didn't train on its query."""
        scores = {name: np.full(len(self.frames[name]), np.nan) for name in ("llm", "serving")}
        for fold, test in enumerate(query_folds):
            booster = self.fit(self.masks(test), self.base)
            for name in scores:
                held_out = self.frames[name]["query"].isin(test).to_numpy()
                scores[name][held_out] = booster.predict(xgb.DMatrix(self.base[name][held_out]))
            print(f"stage-1 out-of-fold {fold} done", flush=True)
        assert not any(np.isnan(values).any() for values in scores.values()), "rows outside every fold"
        return scores

    def masked(self, out_of_fold: dict[str, np.ndarray], k: int) -> dict[str, np.ndarray]:
        """The features with the judge present only in each training query's stage-1 top k."""
        present = {name: top_k(self.queries[name], out_of_fold[name], self.keep[name], k) for name in out_of_fold}
        present["new"] = present["serving"][self.new_rows]
        present["ext"] = np.zeros(len(self.frames["ext"]), dtype=bool)
        return {name: with_judge(self.base[name], self.judge[name], present[name]) for name in self.frames}


def run(constraints: str):
    data = Training(CONSTRAINTS[constraints])
    frames, base, judge, keep = data.frames, data.base, data.judge, data.keep
    manifest = json.loads(MANIFEST.read_text())
    eval_queries = {normalize(q) for q in manifest["llm_eval_queries"]}

    # The same folds as minilm_experiment.py.
    llm = frames["llm"]
    llm_eval = llm["query"].map(normalize).isin(eval_queries).to_numpy()
    queries = np.array(sorted(llm.loc[llm_eval, "query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    folds = np.array_split(queries, FOLDS)

    # The judge-train queries are spread over the out-of-fold folds.
    other = np.array(sorted(set(llm["query"]) - set(queries)))
    np.random.default_rng(1).shuffle(other)
    stage1_folds = [
        set(eval_fold) | set(other_fold) for eval_fold, other_fold in zip(folds, np.array_split(other, FOLDS))
    ]
    out_of_fold = data.out_of_fold(stage1_folds)

    arms = ["base", "all candidates"] + [f"top {k}, {kind}" for k in DEPTHS for kind in ("masked", "full")]
    results = {arm: {"original": {}, "serving": {}} for arm in arms}
    full_feats = data.full

    for fold, test_queries in enumerate(folds):
        test = set(test_queries)
        masks = data.masks(test)
        stage1 = data.fit(masks, base)
        full = data.fit(masks, full_feats)
        stage2 = {k: data.fit(masks, data.masked(out_of_fold, k)) for k in DEPTHS}

        for population, name in [("original", "llm"), ("serving", "serving")]:
            rows = frames[name]["query"].isin(test).to_numpy()
            frame = frames[name][rows].reset_index(drop=True)
            feats = full_feats[name][rows]
            stage1_scores = stage1.predict(xgb.DMatrix(base[name][rows]))
            full_scores = full.predict(xgb.DMatrix(feats))
            results["base"][population].update(evaluate(frame, feats, stage1_scores))
            results["all candidates"][population].update(evaluate(frame, feats, full_scores))
            for k in DEPTHS:
                present = top_k(data.queries[name][rows], stage1_scores, keep[name][rows], k)
                masked_scores = stage2[k].predict(xgb.DMatrix(with_judge(base[name][rows], judge[name][rows], present)))
                for kind, scores in [("masked", masked_scores), ("full", full_scores)]:
                    cascade = np.where(present, TOP_K_OFFSET + scores, stage1_scores)
                    results[f"top {k}, {kind}"][population].update(evaluate(frame, feats, cascade))
        print(f"fold {fold} done", flush=True)

    out_path(constraints).write_text(json.dumps(results))
    report(results)


def interval(diff: np.ndarray, rng: np.random.Generator) -> str:
    means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
    return f"{diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, {np.percentile(means, 97.5):+.4f}]"


def compare(constraints: str):
    """Each arm under the constraints against the same arm unconstrained, query by query."""
    reference = json.loads(out_path("none").read_text())
    results = json.loads(out_path(constraints).read_text())
    rng = np.random.default_rng(0)
    for population in ("serving", "original"):
        print(f"\n## {population} pool: `{constraints}` monotone against unconstrained\n")
        print("| Arm | NDCG@10 | vs unconstrained, 95% CI | Staan inversions | index in top 10 |")
        print("|---|---|---|---|---|")
        queries = sorted(q for q, v in reference["base"][population].items() if not np.isnan(v["ndcg@10"]))
        for arm in results:
            scores = np.array([results[arm][population][q]["ndcg@10"] for q in queries])
            unconstrained = np.array([reference[arm][population][q]["ndcg@10"] for q in queries])
            inversions = np.nanmean([results[arm][population][q]["staan_inversions"] for q in queries])
            before = np.nanmean([reference[arm][population][q].get("staan_inversions", np.nan) for q in queries])
            index = np.nanmean([results[arm][population][q]["index_in_top10"] for q in queries])
            print(
                f"| {arm} | {scores.mean():.4f} | {interval(scores - unconstrained, rng)} | "
                f"{inversions:.1%} (was {before:.1%}) | {index:.1%} |"
            )


def report(results: dict[str, dict[str, dict]]):
    rng = np.random.default_rng(0)
    for population in ("serving", "original"):
        print(f"\n## {population} pool, 5-fold CV over the 424 judge-eval queries, `all` training\n")
        print("| Arm | NDCG@10 | vs base, 95% CI | vs all candidates, 95% CI | weak in top 10 |")
        print("|---|---|---|---|---|")
        queries = sorted(q for q, v in results["base"][population].items() if not np.isnan(v["ndcg@10"]))

        def values(arm: str) -> np.ndarray:
            return np.array([results[arm][population][q]["ndcg@10"] for q in queries])

        for arm in results:
            scores = values(arm)
            weak = np.nanmean([results[arm][population][q]["weak_in_top10"] for q in queries])
            print(
                f"| {arm} | {scores.mean():.4f} | {interval(scores - values('base'), rng)} | "
                f"{interval(scores - values('all candidates'), rng)} | {weak:.1%} |"
            )


if __name__ == "__main__":
    if sys.argv[1:2] == ["compare"]:
        compare(sys.argv[2])
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else "none")
