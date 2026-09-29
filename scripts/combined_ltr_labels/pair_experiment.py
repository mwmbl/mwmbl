"""Why the index-vs-Staan pair classifier in `interleave_experiment.py` did no better than
the ranking model, and whether a pair model can choose the index results instead.

    oof      5-fold cross-validation over the 848 en-gb serving-pool queries, the folds of
             `interleave_experiment.oof`. Trains `ndcg+new` (en-gb Staan) per fold, and pair
             classifiers on the serving pool of the other folds, and caches the held-out
             scores -> pair_oof.json
    auc      how well each pair model, and the ranking model's score difference, orders
             held-out (index, Staan) pairs, overall and for the pairs a merge decides
    report   scores the interleaving arms on the cached scores

Pair models (i is an index result, j a Staan result of the same query):

- `concat`: `interleave_experiment`'s: the two feature rows side by side, every pair.
- `diff`: adds their difference, both ranking-model scores and the query's context: how
  many results Staan returned and how well the model scores Staan's best. The ranking
  scores on training queries are out-of-fold too, so the pair model sees them as it would
  at serving. Shallower trees and a lower learning rate.
- `top`: `diff`, trained only on pairs whose index result is among the model's top three
  for its query: the only ones a merge ever decides.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import sys
from collections.abc import Callable
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from scripts.combined_ltr_labels.engb_staan_experiment import load_engb
from scripts.combined_ltr_labels.interleave_experiment import (
    MARGINS,
    THRESHOLDS,
    Candidate,
    merge,
    optimal_merge,
    report,
    split,
)
from scripts.combined_ltr_labels.objective_experiment import FOLDS, NUM_ROUNDS, TREE_PARAMS, features, kept, train

LABELS = Path("devdata/combined_ltr_labels")
OUT = LABELS / "pair_oof.json"
VARIANTS = ["concat", "diff", "top"]
TOP = 3
CONCAT_PARAMS = {**TREE_PARAMS, "tree_method": "hist", "objective": "binary:logistic"}
DIFF_PARAMS = {**CONCAT_PARAMS, "eta": 0.1, "max_depth": 4, "min_child_weight": 5.0}
DIFF_ROUNDS = 300


def fold_queries() -> list[set[str]]:
    """The folds of `interleave_experiment.oof`."""
    _, _, _, _, llm_gb, _, _ = load_engb()
    queries = np.array(sorted(llm_gb["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    return [set(fold) for fold in np.array_split(queries, FOLDS)]


def ranker_scores(frames: dict[str, pd.DataFrame], feats: dict[str, np.ndarray], excluded: set[str]) -> np.ndarray:
    """Trains `ndcg+new` (en-gb Staan) without `excluded`'s queries and scores the serving pool."""
    excluded_norm = {q.lower().strip() for q in excluded}
    masks = {
        "llm_gb": ~frames["llm_gb"]["query"].isin(excluded).to_numpy(),
        "ext": ~frames["ext"]["query"].str.lower().str.strip().isin(excluded_norm).to_numpy(),
        "new_gb": ~frames["new_gb"]["query"].isin(excluded).to_numpy(),
    }
    frame = pd.concat([frames[n][m] for n, m in masks.items()], ignore_index=True)
    booster = train("ndcg", frame, np.concatenate([feats[n][m] for n, m in masks.items()]))
    return booster.predict(xgb.DMatrix(feats["serving_gb"]))


def pair_table(serving: pd.DataFrame, feats: np.ndarray, scores: np.ndarray, keep: np.ndarray) -> pd.DataFrame:
    """Every (kept index result, Staan result) pair of each query, with the index result's
    rank among the query's kept index results by `scores`."""
    from_staan = serving["from_staan"].to_numpy()
    rows = []
    for query, index in serving.groupby("query").indices.items():
        pool = [i for i in index if keep[i] and not from_staan[i]]
        by_score = sorted(pool, key=lambda i: -scores[i])
        staan = [j for j in index if from_staan[j]]
        best_staan = max(scores[j] for j in staan) if staan else 0.0
        for index_rank, i in enumerate(by_score):
            for j in staan:
                rows.append((query, i, j, index_rank, len(staan), len(pool), best_staan))
    return pd.DataFrame(rows, columns=["query", "i", "j", "index_rank", "staan_count", "index_count", "best_staan"])


def pair_features(table: pd.DataFrame, feats: np.ndarray, scores: np.ndarray, variant: str) -> np.ndarray:
    left, right = feats[table["i"]], feats[table["j"]]
    if variant == "concat":
        return np.concatenate([left, right], axis=1)
    score_i, score_j = scores[table["i"]], scores[table["j"]]
    context = np.column_stack(
        [
            score_i,
            score_j,
            score_i - score_j,
            score_i - table["best_staan"],
            table["index_rank"],
            table["staan_count"],
            table["index_count"],
        ]
    )
    return np.concatenate([left, right, left - right, context], axis=1)


def train_pairs(table: pd.DataFrame, feats: np.ndarray, scores: np.ndarray, grades: np.ndarray, variant: str):
    if variant == "top":
        table = table[table["index_rank"] < TOP]
    gap = grades[table["i"].to_numpy()] - grades[table["j"].to_numpy()]
    useful = gap != 0
    rows = pair_features(table, feats, scores, variant)[useful]
    labels = (gap[useful] > 0).astype(float)
    weights = np.abs(gap[useful])
    params, rounds = (CONCAT_PARAMS, NUM_ROUNDS) if variant == "concat" else (DIFF_PARAMS, DIFF_ROUNDS)
    return xgb.train(params, xgb.DMatrix(rows, label=labels, weight=weights), rounds)


def oof():
    _, ext, _, _, llm_gb, new_gb, serving = load_engb()
    frames = {"llm_gb": llm_gb, "ext": ext, "new_gb": new_gb, "serving_gb": serving}
    feats = {name: features(frame) for name, frame in frames.items()}
    serving_feats = feats["serving_gb"]
    keep = kept(serving, serving_feats)
    grades = serving["overall"].to_numpy().astype(float)
    folds = fold_queries()
    fold_of = serving["query"].map({q: f for f, qs in enumerate(folds) for q in qs}).to_numpy()

    # A model trained without folds f and g scores fold g's queries out of fold for the
    # pair model trained on outer fold f's training queries: 5 + 10 rankers, not 5 + 20.
    outer = {f: ranker_scores(frames, feats, folds[f]) for f in range(FOLDS)}
    inner = {
        pair: ranker_scores(frames, feats, folds[pair[0]] | folds[pair[1]]) for pair in combinations(range(FOLDS), 2)
    }
    print("rankers trained", flush=True)

    out: dict[str, dict] = {}
    for fold in range(FOLDS):
        train_scores = np.zeros(len(serving))
        for other in range(FOLDS):
            if other != fold:
                train_scores[fold_of == other] = inner[(min(fold, other), max(fold, other))][fold_of == other]
        test_scores = outer[fold]
        train_table = pair_table(serving, serving_feats, train_scores, keep)
        train_table = train_table[fold_of[train_table["i"]] != fold]
        test_table = pair_table(serving, serving_feats, test_scores, keep)
        test_table = test_table[fold_of[test_table["i"]] == fold]
        predictions = {}
        for variant in VARIANTS:
            booster = train_pairs(train_table, serving_feats, train_scores, grades, variant)
            predictions[variant] = booster.predict(
                xgb.DMatrix(pair_features(test_table, serving_feats, test_scores, variant))
            )
            print(f"fold {fold} {variant}", flush=True)

        beats: dict[int, dict[str, dict[str, float]]] = {}
        for k, (i, j) in enumerate(zip(test_table["i"], test_table["j"])):
            for variant, values in predictions.items():
                beats.setdefault(i, {}).setdefault(variant, {})[str(int(serving["staan_rank"].iloc[j]))] = float(
                    values[k]
                )
        held = np.flatnonzero((fold_of == fold) & keep)
        for query, index in serving.iloc[held].groupby("query").indices.items():
            out[query] = {
                "fold": fold,
                "candidates": [
                    {
                        "grade": int(grades[held[k]]),
                        "score": float(test_scores[held[k]]),
                        "staan_rank": int(serving["staan_rank"].iloc[held[k]])
                        if serving["from_staan"].iloc[held[k]]
                        else None,
                        "beats": beats.get(held[k], {}),
                    }
                    for k in index
                ],
            }
    OUT.write_text(json.dumps(out))


def auc():
    data = json.loads(OUT.read_text())
    rows = []
    for v in data.values():
        staan, index = split(v["candidates"])
        for index_rank, i in enumerate(index):
            for position, j in enumerate(staan):
                if i["grade"] == j["grade"]:
                    continue
                rows.append(
                    {
                        "index_rank": index_rank,
                        "staan_position": position,
                        "label": i["grade"] > j["grade"],
                        "gap": abs(i["grade"] - j["grade"]),
                        "ranker": i["score"] - j["score"],
                        **{variant: i["beats"][variant][str(j["staan_rank"])] for variant in VARIANTS},
                    }
                )
    pairs = pd.DataFrame(rows)
    subsets = {
        "all pairs": pairs,
        f"index top {TOP}": pairs[pairs["index_rank"] < TOP],
        "index top 1": pairs[pairs["index_rank"] == 0],
        "index top 1, Staan #6 onward": pairs[(pairs["index_rank"] == 0) & (pairs["staan_position"] >= 5)],
    }
    print("Held-out pairwise AUC (untied pairs; weighted by the grade gap in brackets)\n")
    print("| Pairs | n | index wins | ranker | " + " | ".join(VARIANTS) + " |")
    print("|---|---|---|---|" + "---|" * len(VARIANTS))
    for name, subset in subsets.items():
        cells = [
            f"{roc_auc_score(subset['label'], subset[m]):.3f} "
            f"({roc_auc_score(subset['label'], subset[m], sample_weight=subset['gap']):.3f})"
            for m in ["ranker", *VARIANTS]
        ]
        print(f"| {name} | {len(subset)} | {subset['label'].mean():.2f} | " + " | ".join(cells) + " |")

    print("\nCalibration on index top 1 (predicted vs observed win rate):\n")
    top1 = subsets["index top 1"]
    for variant in VARIANTS:
        bins = pd.cut(top1[variant], [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0])
        table = top1.groupby(bins, observed=True).agg(
            n=("label", "size"), predicted=(variant, "mean"), won=("label", "mean")
        )
        cells = ", ".join(f"{b}: {r.predicted:.2f}→{r.won:.2f} (n={r.n})" for b, r in table.iterrows())
        print(f"- {variant}: {cells}")


def win_rate(candidate: Candidate, variant: str) -> float:
    """The index result's mean probability of beating each of the query's Staan results."""
    beats = candidate["beats"].get(variant)
    return float(np.mean(list(beats.values()))) if beats else candidate["score"]


def pair_merge(threshold: float, variant: str, order: str, rest: bool = False) -> Callable:
    """The next index result, in the `order` key's order, goes in when it beats the next Staan
    result (with `rest`, every remaining one) with probability above `threshold`."""

    def arm(candidates: list[Candidate]) -> list[Candidate]:
        staan, index = split(candidates, order)
        ranked = []
        while staan and index:
            beats = index[0]["beats"][variant]
            rivals = staan if rest else staan[:1]
            if len(ranked) < 10 and min(beats[str(c["staan_rank"])] for c in rivals) > threshold:
                ranked.append(index.pop(0))
            else:
                ranked.append(staan.pop(0))
        return ranked + staan + index

    return arm


def with_win_rates(path: Path) -> Path:
    """Adds each variant's win rate to the cached candidates, for `split` to order by."""
    data = json.loads(path.read_text())
    for v in data.values():
        for c in v["candidates"]:
            if c["staan_rank"] is None:
                c.update({f"{variant} win": win_rate(c, variant) for variant in VARIANTS})
            else:
                c.update({f"{variant} win": 0.0 for variant in VARIANTS})
    scored = path.with_name(path.stem + "_win.json")
    scored.write_text(json.dumps(data))
    return scored


def grids() -> dict[str, dict[str, Callable]]:
    families: dict[str, dict[str, Callable]] = {
        "staan-first": {"": lambda candidates: sum(split(candidates), [])},
        "merge-rest": {f"merge-rest {d}": merge(d, rest=True) for d in MARGINS},
        "oracle merge": {"": optimal_merge("grade")},
    }
    for variant in VARIANTS:
        win = f"{variant} win"
        families[f"pair {variant}"] = {f"pair {variant} {t}": pair_merge(t, variant, "score") for t in THRESHOLDS}
        families[f"pair-rest {variant}"] = {
            f"pair-rest {variant} {t}": pair_merge(t, variant, "score", rest=True) for t in THRESHOLDS
        }
        families[f"staan-first, fill by {win}"] = {"": lambda candidates, w=win: sum(split(candidates, w), [])}
        families[f"pair {variant}, by {win}"] = {
            f"pair {variant}, by {win} {t}": pair_merge(t, variant, win) for t in THRESHOLDS
        }
        families[f"pair-rest {variant}, by {win}"] = {
            f"pair-rest {variant}, by {win} {t}": pair_merge(t, variant, win, rest=True) for t in THRESHOLDS
        }
    return families


if __name__ == "__main__":
    commands = {
        "oof": oof,
        "auc": auc,
        "report": lambda: report(with_win_rates(OUT), grids(), show_all=False),
    }
    commands[sys.argv[1]]()
