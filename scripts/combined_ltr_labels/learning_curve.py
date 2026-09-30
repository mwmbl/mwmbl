"""Learning curve for the Combined Search model: NDCG@10 against the number of LLM-labelled
training queries, to see whether more labels would help.

Runs `objective_experiment.py`'s 5-fold cross-validation, but trains each fold on a random
subset of its ~680 training queries. A query's serving-pool labels (`+new`) come and go with
it. The extension dataset is kept whole in every arm: it is cheap to produce, and the
question is what more LLM labels buy. The held-out folds are always complete, so every point
is scored on the same 849 queries. Each fraction below 1 is repeated over several seeds.

Writes `devdata/combined_ltr_labels/learning_curve.json` and `learning_curve.png`.
"""

import json

import matplotlib
import numpy as np
import pandas as pd

from scripts.combined_ltr_labels.objective_experiment import FOLDS, LABELS, features, load, score_population, train

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = LABELS / "learning_curve.json"
PLOT = LABELS / "learning_curve.png"
FRACTIONS = [0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0]
SEEDS = 3
ARMS = [("binary", False), ("ndcg", True)]


def arm_name(objective: str, with_new: bool) -> str:
    return f"{objective}{'+new' if with_new else ''}"


def run():
    llm, ext, new, serving = load()
    feats = {name: features(frame) for name, frame in [("llm", llm), ("ext", ext), ("new", new), ("serving", serving)]}

    queries = np.array(sorted(llm["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    folds = np.array_split(queries, FOLDS)

    results = []
    for fraction in FRACTIONS:
        for seed in range(SEEDS if fraction < 1 else 1):
            rng = np.random.default_rng(seed)
            per_arm: dict[str, dict[str, list[float]]] = {
                arm_name(*arm): {"original": [], "serving": []} for arm in ARMS
            }
            train_sizes = []
            for test_queries in folds:
                test = set(test_queries)
                test_norm = {q.lower().strip() for q in test}
                train_pool = np.array([q for q in queries if q not in test])
                sample_size = round(fraction * len(train_pool))
                sample = set(rng.choice(train_pool, sample_size, replace=False))
                train_sizes.append(sample_size)

                llm_train = llm["query"].isin(sample)
                ext_train = ~ext["query"].str.lower().str.strip().isin(test_norm)
                new_train = new["query"].isin(sample)
                llm_test = llm["query"].isin(test).to_numpy()
                serving_test = serving["query"].isin(test).to_numpy()
                for objective, with_new in ARMS:
                    parts = [
                        (llm[llm_train], feats["llm"][llm_train.to_numpy()]),
                        (ext[ext_train], feats["ext"][ext_train.to_numpy()]),
                    ]
                    if with_new:
                        parts.append((new[new_train], feats["new"][new_train.to_numpy()]))
                    frame = pd.concat([p[0] for p in parts], ignore_index=True)
                    booster = train(objective, frame, np.concatenate([p[1] for p in parts]))
                    populations = {
                        "original": score_population(
                            booster, llm[llm_test].reset_index(drop=True), feats["llm"][llm_test]
                        ),
                        "serving": score_population(
                            booster, serving[serving_test].reset_index(drop=True), feats["serving"][serving_test]
                        ),
                    }
                    for population, scores in populations.items():
                        per_arm[arm_name(objective, with_new)][population].extend(
                            v["ndcg@10"] for v in scores.values() if not np.isnan(v["ndcg@10"])
                        )
            for arm, populations in per_arm.items():
                row = {
                    "fraction": fraction,
                    "seed": seed,
                    "train_queries": float(np.mean(train_sizes)),
                    "arm": arm,
                    **{population: float(np.mean(values)) for population, values in populations.items()},
                }
                results.append(row)
                print(json.dumps(row), flush=True)

    OUT.write_text(json.dumps(results, indent=1))
    plot(results)


def plot(results: list[dict]):
    frame = pd.DataFrame(results)
    figure, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharex=True)
    colours = {"binary": "#2a6fdb", "ndcg+new": "#d9534f"}
    for axis, population, title in [
        (axes[0], "serving", "Serving pool (what production ranks)"),
        (axes[1], "original", "Original Pass-2 pool"),
    ]:
        for arm, rows in frame.groupby("arm"):
            summary = rows.groupby("train_queries")[population].agg(["mean", "min", "max"]).reset_index()
            axis.plot(summary["train_queries"], summary["mean"], marker="o", color=colours[arm], label=arm)
            axis.fill_between(summary["train_queries"], summary["min"], summary["max"], color=colours[arm], alpha=0.15)
        axis.set_title(title)
        axis.set_xlabel("LLM-labelled training queries per fold")
        axis.set_ylabel("NDCG@10 (5-fold CV)")
        axis.grid(alpha=0.3)
        axis.legend()
    figure.suptitle("Combined Search LTR learning curve (band: min–max over 3 subsamples)")
    figure.tight_layout()
    figure.savefig(PLOT, dpi=130)


if __name__ == "__main__":
    run()
