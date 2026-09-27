"""The fine-tuned MiniLM judges' scores as features of the rank:ndcg+new Combined Search model.

Each of the three judges in `minilm_scores.json` (`both`, `pointwise`, `pairs`) adds one
feature, and the arms try each alone and all three together.

The judges were fine-tuned on the `overall` grades of 340 of the 849 queries and
checkpoint-selected on 85 more (`devdata/judge_train/eval_manifest.json`), and their
curation pairs exclude those 509 queries' held-out part. So:

- Every arm is cross-validated over the 424 `llm_eval_queries` only, which no judge has
  seen in any form.
- `clean` arms train on the other eval-query folds alone (plus the extension rows).
- `all` arms also train on the 425 judge-train/val queries. Their MiniLM scores are
  in-sample and look better than they would at serving, which can only mislead the model,
  not the test, so these arms measure whether that hurts.

The extension rows aren't scored, so their MiniLM features are missing, and XGBoost learns
a default branch for them. Every test row has all features.

Writes `devdata/combined_ltr_labels/minilm_experiment.json` with per-query scores.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.combined_ltr_labels.objective_experiment import features, load, score_population, train

LABELS = Path("devdata/combined_ltr_labels")
SCORES = LABELS / "minilm_scores.json"
MANIFEST = Path("devdata/judge_train/eval_manifest.json")
OUT = LABELS / "minilm_experiment.json"

FOLDS = 5
OBJECTIVE = "ndcg"
FEATURE_SETS = {
    "base": [],
    "both": ["minilm-both-v1"],
    "pointwise": ["minilm-pointwise-v1"],
    "pairs": ["minilm-pairs-v1"],
    "all3": ["minilm-both-v1", "minilm-pointwise-v1", "minilm-pairs-v1"],
}
TRAINING_SETS = ["clean", "all"]


def normalize(query: str) -> str:
    return " ".join(query.lower().split())


def minilm_columns(frame: pd.DataFrame, scores: dict[str, dict[str, float]]) -> np.ndarray:
    """One column per judge, NaN where the pair wasn't scored (the extension rows)."""
    keys = [f"{query}\t{url}" for query, url in zip(frame["query"], frame["url"])]
    return np.array([[scores[model].get(key, np.nan) for model in scores] for key in keys], dtype=np.float32)


def run():
    scores = json.loads(SCORES.read_text())
    models = list(scores)
    manifest = json.loads(MANIFEST.read_text())
    eval_queries = {normalize(q) for q in manifest["llm_eval_queries"]}

    llm, ext, new, serving = load()
    frames = {"llm": llm, "ext": ext, "new": new, "serving": serving}
    base_feats = {name: features(frame) for name, frame in frames.items()}
    judge_feats = {name: minilm_columns(frame, scores) for name, frame in frames.items()}
    for name in ("llm", "new", "serving"):
        assert not np.isnan(judge_feats[name]).any(), f"unscored {name} pairs"

    def feats(name: str, mask: np.ndarray, feature_set: str) -> np.ndarray:
        columns = [models.index(model) for model in FEATURE_SETS[feature_set]]
        return np.concatenate([base_feats[name][mask], judge_feats[name][mask][:, columns]], axis=1)

    llm_eval = llm["query"].map(normalize).isin(eval_queries).to_numpy()
    new_eval = new["query"].map(normalize).isin(eval_queries).to_numpy()
    queries = np.array(sorted(llm.loc[llm_eval, "query"].unique()))
    print(f"{len(queries)} eval queries, {llm['query'].nunique() - len(queries)} judge-train/val queries")
    np.random.default_rng(0).shuffle(queries)
    folds = np.array_split(queries, FOLDS)

    arms = [(training, feature_set) for training in TRAINING_SETS for feature_set in FEATURE_SETS]
    results = {f"{t}/{f}": {"original": {}, "serving": {}} for t, f in arms}
    results["minilm-both alone"] = {"original": {}, "serving": {}}

    for fold, test_queries in enumerate(folds):
        test = set(test_queries)
        test_norm = {normalize(q) for q in test}
        llm_test = llm["query"].isin(test).to_numpy()
        serving_test = serving["query"].isin(test).to_numpy()
        ext_train = ~ext["query"].map(normalize).isin(test_norm).to_numpy()
        for training, feature_set in arms:
            restrict = training == "clean"
            llm_train = ~llm_test & (llm_eval if restrict else True)
            new_train = ~new["query"].isin(test).to_numpy() & (new_eval if restrict else True)
            frame = pd.concat([llm[llm_train], ext[ext_train], new[new_train]], ignore_index=True)
            train_feats = np.concatenate(
                [
                    feats("llm", llm_train, feature_set),
                    feats("ext", ext_train, feature_set),
                    feats("new", new_train, feature_set),
                ]
            )
            booster = train(OBJECTIVE, frame, train_feats)
            arm = f"{training}/{feature_set}"
            results[arm]["original"].update(
                score_population(booster, llm[llm_test].reset_index(drop=True), feats("llm", llm_test, feature_set))
            )
            results[arm]["serving"].update(
                score_population(
                    booster, serving[serving_test].reset_index(drop=True), feats("serving", serving_test, feature_set)
                )
            )
            print(f"fold {fold} {arm} done", flush=True)

    alone = MinilmAlone(models.index("minilm-both-v1"), base_feats["llm"].shape[1])
    for population, name, frame in [("original", "llm", llm), ("serving", "serving", serving)]:
        test = frame["query"].map(normalize).isin(eval_queries).to_numpy()
        results["minilm-both alone"][population] = score_population(
            alone, frame[test].reset_index(drop=True), feats(name, test, "all3")
        )

    OUT.write_text(json.dumps(results))
    report(results)


class MinilmAlone:
    """Orders candidates by one judge's score, through the same filter and metric."""

    def __init__(self, model_index: int, num_base_features: int):
        self.column = num_base_features + model_index

    def predict(self, dmatrix) -> np.ndarray:
        return dmatrix.get_data().toarray()[:, self.column]


def report(results: dict[str, dict[str, dict]]):
    rng = np.random.default_rng(0)
    for population in ("serving", "original"):
        print(f"\n## {population} pool, 5-fold CV over the 424 judge-eval queries\n")
        print("| Arm | NDCG@10 | vs base, 95% CI | weak in top 10 | index in top 10 |")
        print("|---|---|---|---|---|")
        for training in [*TRAINING_SETS, None]:
            base = results["clean/base" if training is None else f"{training}/base"][population]
            queries = sorted(q for q, v in base.items() if not np.isnan(v["ndcg@10"]))
            baseline = np.array([base[q]["ndcg@10"] for q in queries])
            arms = [a for a in results if a.startswith(f"{training}/")] if training else ["minilm-both alone"]
            for arm in arms:
                scores = results[arm][population]
                values = np.array([scores[q]["ndcg@10"] for q in queries])
                diff = values - baseline
                means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
                weak = np.nanmean([scores[q]["weak_in_top10"] for q in queries])
                index = np.nanmean([scores[q]["index_in_top10"] for q in queries])
                print(
                    f"| {arm} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
                    f"{np.percentile(means, 97.5):+.4f}] | {weak:.1%} | {index:.1%} |"
                )


if __name__ == "__main__":
    run()
