"""Interleave index results into Staan's list without reordering Staan.

Staan-first (Staan's results in Staan's order, then the model's index results) wins the
holistic comparison against every learned ordering, even ones that tie or beat it on NDCG.
These arms keep that property: Staan's results always appear in Staan's order, and index
results in the model's order. They differ only in where index results are let in.

    oof      5-fold cross-validation, as `engb_staan_experiment.cv`: trains `ndcg+new`
             (en-gb Staan) per fold and caches the held-out scores of the en-gb serving pool
             -> interleave_oof.json
    report   scores every arm on the cached scores

Arms (s(x) is the model's score; the majority-terms filter still drops index results):

- `staan-first`: the baseline.
- `slot p`: the model's top index result at position p, Staan-first around it.
- `merge d`: a two-list merge. The next index result goes before the next Staan result
  when it outscores it by more than d.
- `merge-rest d`: as `merge`, but the index result must outscore every remaining Staan
  result by more than d, so a weak Staan result can't let one in ahead of a strong one.
- `merge d, protect k`: `merge d` with Staan's top k fixed at the top.
- `merge d, cap m`: `merge d` with at most m index results in the top ten.
- `learned`: the model's own order, for reference; it doesn't preserve Staan's order.

An arm with a parameter grid is also scored `(tuned)`: nested cross-validation picks its
parameter on the other four folds' queries.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from mwmbl.tinysearchengine.super_search_select.judge import Judge, doc_text
from scripts.combined_ltr_labels.engb_staan_experiment import STAAN_ENGB, load_engb
from scripts.combined_ltr_labels.minilm_experiment import MANIFEST, minilm_columns, normalize
from scripts.combined_ltr_labels.minilm_scores import MODELS
from scripts.combined_ltr_labels.minilm_scores import OUT as MINILM_SCORES
from scripts.combined_ltr_labels.objective_experiment import (
    EXT_WEIGHT,
    FOLDS,
    NUM_ROUNDS,
    TREE_PARAMS,
    features,
    kept,
    read_jsonl,
    train,
)

LABELS = Path("devdata/combined_ltr_labels")
OOF = LABELS / "interleave_oof.json"
JUDGE_OOF = LABELS / "interleave_judge_oof.json"
JUDGE = "minilm-both-v1"
TRAINING_SETS = ["clean", "all"]
PAIR_PARAMS = {**TREE_PARAMS, "tree_method": "hist", "objective": "binary:logistic"}
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.975, 0.99]
JUDGE_MARGINS = [-0.2, -0.1, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5]
DISCOUNTS = 1 / np.log2(np.arange(2, 12))

MARGINS = [-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
SLOTS = list(range(1, 11))
PROTECT = [1, 2, 3, 5]
CAPS = [1, 2, 3]

Candidate = dict  # {"grade", "score", "staan_rank"}; staan_rank is None for the index


def oof():
    _, ext, _, _, llm_gb, new_gb, serving_gb = load_engb()
    frames = {"llm_gb": llm_gb, "ext": ext, "new_gb": new_gb, "serving_gb": serving_gb}
    feats = {name: features(frame) for name, frame in frames.items()}
    queries = np.array(sorted(llm_gb["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    out: dict[str, dict] = {}
    for fold, test_queries in enumerate(np.array_split(queries, FOLDS)):
        test = set(test_queries)
        test_norm = {q.lower().strip() for q in test}
        masks = {
            "llm_gb": ~llm_gb["query"].isin(test),
            "ext": ~ext["query"].str.lower().str.strip().isin(test_norm),
            "new_gb": ~new_gb["query"].isin(test),
        }
        frame = pd.concat([frames[n][m] for n, m in masks.items()], ignore_index=True)
        train_feats = np.concatenate([feats[n][m.to_numpy()] for n, m in masks.items()])
        booster = train("ndcg", frame, train_feats)
        weights = np.where(frame["source"] == "ext", EXT_WEIGHT, 1.0)
        regression = xgb.train(
            {**TREE_PARAMS, "objective": "reg:squarederror"},
            xgb.DMatrix(train_feats, label=frame["overall"].to_numpy(), weight=weights),
            NUM_ROUNDS,
        )
        held_mask = serving_gb["query"].isin(test).to_numpy()
        held = serving_gb[held_mask].reset_index(drop=True)
        held_feats = feats["serving_gb"][held_mask]
        scores = booster.predict(xgb.DMatrix(held_feats))
        predicted = regression.predict(xgb.DMatrix(held_feats))
        keep = kept(held, held_feats)
        for query, index in held.groupby("query").indices.items():
            out[query] = {
                "fold": fold,
                "candidates": [
                    {
                        "grade": int(held["overall"].iloc[i]),
                        "score": float(scores[i]),
                        "predicted": float(predicted[i]),
                        "staan_rank": None if not held["from_staan"].iloc[i] else int(held["staan_rank"].iloc[i]),
                    }
                    for i in index
                    if keep[i]
                ],
            }
        print(f"fold {fold}", flush=True)
    OOF.write_text(json.dumps(out))


def minilm_engb():
    """Scores the en-gb Staan results `minilm_scores.json` lacks with the `JUDGE` MiniLM."""
    scores = json.loads(MINILM_SCORES.read_text())
    judged = scores[JUDGE]
    judge = Judge(MODELS / JUDGE / "onnx")
    records = read_jsonl(STAAN_ENGB)
    for i, record in enumerate(records):
        query = record["query"]
        missing = [r for r in record["results"] if f"{query}\t{r['url']}" not in judged]
        values = judge.score(query, [doc_text(r["title"], r["extract"]) for r in missing])
        judged.update({f"{query}\t{r['url']}": value for r, value in zip(missing, values)})
        if i % 100 == 0:
            print(f"{i}/{len(records)}", flush=True)
    MINILM_SCORES.write_text(json.dumps(scores))


def pair_rows(
    frame: pd.DataFrame, feats: np.ndarray, keep: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[tuple[int, int]]]:
    """Every (index result, Staan result) pair of each query: the two feature rows side by
    side, whether the index result grades higher, its weight (the grade gap), and the pair's
    row numbers. Tied pairs carry no weight."""
    from_staan = frame["from_staan"].to_numpy()
    grades = frame["overall"].to_numpy().astype(float)
    pairs = [
        (i, j)
        for index in frame.groupby("query").indices.values()
        for i in index
        if keep[i] and not from_staan[i]
        for j in index
        if from_staan[j]
    ]
    left = np.array([i for i, _ in pairs], dtype=int)
    right = np.array([j for _, j in pairs], dtype=int)
    rows = np.concatenate([feats[left], feats[right]], axis=1)
    return rows, (grades[left] > grades[right]).astype(float), np.abs(grades[left] - grades[right]), pairs


def train_pairs(frame: pd.DataFrame, feats: np.ndarray, keep: np.ndarray) -> xgb.Booster:
    rows, labels, weights, _ = pair_rows(frame, feats, keep)
    useful = weights > 0
    return xgb.train(PAIR_PARAMS, xgb.DMatrix(rows[useful], label=labels[useful], weight=weights[useful]), NUM_ROUNDS)


def judge_oof():
    """Cross-validation over the 424 queries no MiniLM judge saw, as `minilm_experiment`.

    Per fold and training set, trains `ndcg+new` (en-gb Staan) with and without the judge as
    a feature, and index-vs-Staan pair classifiers with and without it, on the serving pool.
    """
    _, ext, _, _, llm_gb, new_gb, serving_gb = load_engb()
    scores = {JUDGE: json.loads(MINILM_SCORES.read_text())[JUDGE]}
    frames = {"llm_gb": llm_gb, "ext": ext, "new_gb": new_gb, "serving_gb": serving_gb}
    base = {name: features(frame) for name, frame in frames.items()}
    judged = {name: minilm_columns(frame, scores) for name, frame in frames.items()}
    for name in ("llm_gb", "new_gb", "serving_gb"):
        assert not np.isnan(judged[name]).any(), f"unscored {name} pairs"
    with_judge = {name: np.concatenate([base[name], judged[name]], axis=1) for name in frames}
    keep = kept(serving_gb, base["serving_gb"])

    eval_queries = {normalize(q) for q in json.loads(MANIFEST.read_text())["llm_eval_queries"]}
    is_eval = {name: frame["query"].map(normalize).isin(eval_queries).to_numpy() for name, frame in frames.items()}
    queries = np.array(sorted(llm_gb.loc[is_eval["llm_gb"], "query"].unique()))
    print(f"{len(queries)} eval queries")
    np.random.default_rng(0).shuffle(queries)

    out: dict[str, dict] = {}
    for fold, test_queries in enumerate(np.array_split(queries, FOLDS)):
        test = set(test_queries)
        test_norm = {normalize(q) for q in test}
        held_mask = serving_gb["query"].isin(test).to_numpy()
        held = serving_gb[held_mask].reset_index(drop=True)
        held_keep = keep[held_mask]
        _, _, _, held_pairs = pair_rows(held, base["serving_gb"][held_mask], held_keep)
        predictions: dict[str, np.ndarray] = {}
        pair_predictions: dict[str, np.ndarray] = {}
        for training in TRAINING_SETS:
            restrict = training == "clean"
            masks = {
                "llm_gb": ~llm_gb["query"].isin(test).to_numpy() & (is_eval["llm_gb"] if restrict else True),
                "ext": ~ext["query"].map(normalize).isin(test_norm).to_numpy(),
                "new_gb": ~new_gb["query"].isin(test).to_numpy() & (is_eval["new_gb"] if restrict else True),
            }
            frame = pd.concat([frames[n][m] for n, m in masks.items()], ignore_index=True)
            serving_train = ~serving_gb["query"].isin(test).to_numpy() & (is_eval["serving_gb"] if restrict else True)
            pair_frame = serving_gb[serving_train].reset_index(drop=True)
            for feature_set, feats in (("base", base), ("minilm", with_judge)):
                name = f"{training}/{feature_set}"
                booster = train("ndcg", frame, np.concatenate([feats[n][m] for n, m in masks.items()]))
                predictions[name] = booster.predict(xgb.DMatrix(feats["serving_gb"][held_mask]))
                pairs = train_pairs(pair_frame, feats["serving_gb"][serving_train], keep[serving_train])
                held_rows, _, _, _ = pair_rows(held, feats["serving_gb"][held_mask], held_keep)
                pair_predictions[name] = pairs.predict(xgb.DMatrix(held_rows))
                print(f"fold {fold} {name}", flush=True)

        beats: dict[int, dict[str, dict[str, float]]] = {}
        for k, (i, j) in enumerate(held_pairs):
            for name, values in pair_predictions.items():
                beats.setdefault(i, {}).setdefault(name, {})[str(int(held["staan_rank"].iloc[j]))] = float(values[k])
        judge_scores = judged["serving_gb"][held_mask][:, 0]
        for query, index in held.groupby("query").indices.items():
            out[query] = {
                "fold": fold,
                "candidates": [
                    {
                        "grade": int(held["overall"].iloc[i]),
                        "staan_rank": None if not held["from_staan"].iloc[i] else int(held["staan_rank"].iloc[i]),
                        "minilm": float(judge_scores[i]),
                        **{name: float(values[i]) for name, values in predictions.items()},
                        "beats": beats.get(i, {}),
                    }
                    for i in index
                    if held_keep[i]
                ],
            }
    JUDGE_OOF.write_text(json.dumps(out))


def pair_merge(threshold: float, model: str, rest: bool = False) -> Callable:
    """Merges by the pair classifier: the next index result goes before the next Staan result
    when it beats it with probability above `threshold` (with `rest`, beats every remaining
    Staan result). The index is in the same training set and features' `ndcg` order."""

    def arm(candidates: list[Candidate]) -> list[Candidate]:
        staan, index = split(candidates, model)
        ranked = []
        while staan and index:
            beats = index[0]["beats"][model]
            rivals = staan if rest else staan[:1]
            if len(ranked) < 10 and min(beats[str(c["staan_rank"])] for c in rivals) > threshold:
                ranked.append(index.pop(0))
            else:
                ranked.append(staan.pop(0))
        return ranked + staan + index

    return arm


def judge_grids() -> dict[str, dict[str, Callable]]:
    families: dict[str, dict[str, Callable]] = {
        "staan-first": {"": lambda candidates: split(candidates, "all/base")[0] + split(candidates, "all/base")[1]},
        "oracle merge": {"": optimal_merge("grade")},
    }
    for training in TRAINING_SETS:
        for feature_set in ("base", "minilm"):
            model = f"{training}/{feature_set}"
            families[f"learned {model}"] = {"": lambda candidates, m=model: sorted(candidates, key=lambda c: -c[m])}
            families[f"merge-rest {model}"] = {
                f"merge-rest {model} {d}": merge(d, rest=True, key=model, order=model) for d in MARGINS
            }
            families[f"pair {model}"] = {f"pair {model} {t}": pair_merge(t, model) for t in THRESHOLDS}
            families[f"pair-rest {model}"] = {
                f"pair-rest {model} {t}": pair_merge(t, model, rest=True) for t in THRESHOLDS
            }
    families["minilm merge"] = {f"minilm merge {d}": merge(d, key="minilm", order="all/base") for d in JUDGE_MARGINS}
    families["minilm merge-rest"] = {
        f"minilm merge-rest {d}": merge(d, rest=True, key="minilm", order="all/base") for d in JUDGE_MARGINS
    }
    return families


def split(candidates: list[Candidate], order: str = "score") -> tuple[list[Candidate], list[Candidate]]:
    """Staan's results in Staan's order, and the index's by `order`."""
    staan = sorted((c for c in candidates if c["staan_rank"] is not None), key=lambda c: c["staan_rank"])
    index = sorted((c for c in candidates if c["staan_rank"] is None), key=lambda c: -c[order])
    return staan, index


def staan_first(candidates: list[Candidate]) -> list[Candidate]:
    staan, index = split(candidates)
    return staan + index


def learned(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: -c["score"])


def slot(position: int) -> Callable[[list[Candidate]], list[Candidate]]:
    def arm(candidates: list[Candidate]) -> list[Candidate]:
        staan, index = split(candidates)
        ranked = staan + index[1:]
        return ranked[: position - 1] + index[:1] + ranked[position - 1 :]

    return arm


def merge(
    margin: float, rest: bool = False, protect: int = 0, cap: int = 10, key: str = "score", order: str = "score"
) -> Callable:
    """Merges on `key`, with the index in `order`."""

    def arm(candidates: list[Candidate]) -> list[Candidate]:
        staan, index = split(candidates, order)
        ranked, staan = staan[:protect], staan[protect:]
        inserted = 0
        while staan and index:
            rival = max(c[key] for c in staan) if rest else staan[0][key]
            if inserted < cap and len(ranked) < 10 and index[0][key] > rival + margin:
                ranked.append(index.pop(0))
                inserted += 1
            else:
                ranked.append(staan.pop(0))
        return ranked + staan + index

    return arm


def optimal_merge(key: str, margin: float = 0.0) -> Callable:
    """The merge of Staan's list and the index's that maximises DCG@10 over `key`.

    `key` is the model's predicted grade, or the true grade for an oracle: the best any
    order-preserving interleaving could do. An index result's value is reduced by `margin`.
    """

    def arm(candidates: list[Candidate]) -> list[Candidate]:
        staan, index = split(candidates, key)
        gain_staan = [c[key] for c in staan]
        gain_index = [c[key] - margin for c in index]
        # best[i][j]: the best DCG of the positions after taking i Staan and j index results.
        n, m = min(len(staan), 10), min(len(index), 10)
        best = np.zeros((n + 2, m + 2))
        for i in range(n, -1, -1):
            for j in range(m, -1, -1):
                position = i + j
                if position >= 10:
                    continue
                options = []
                if i < n:
                    options.append(gain_staan[i] * DISCOUNTS[position] + best[i + 1][j])
                if j < m:
                    options.append(gain_index[j] * DISCOUNTS[position] + best[i][j + 1])
                best[i][j] = max(options) if options else 0.0
        ranked, i, j = [], 0, 0
        while i + j < 10 and (i < n or j < m):
            position = i + j
            take_staan = j >= m or (
                i < n
                and gain_staan[i] * DISCOUNTS[position] + best[i + 1][j]
                >= gain_index[j] * DISCOUNTS[position] + best[i][j + 1]
            )
            if take_staan:
                ranked.append(staan[i])
                i += 1
            else:
                ranked.append(index[j])
                j += 1
        return ranked + staan[i:] + index[j:]

    return arm


def ndcg(candidates: list[Candidate], ranked: list[Candidate]) -> float:
    ideal = np.sort([c["grade"] for c in candidates])[::-1][:10]
    top = np.array([c["grade"] for c in ranked[:10]], dtype=float)
    return float(np.sum(top * DISCOUNTS[: len(top)]) / np.sum(ideal * DISCOUNTS[: len(ideal)]))


def index_in_top10(ranked: list[Candidate]) -> float:
    top = ranked[:10]
    return sum(c["staan_rank"] is None for c in top)


def grids() -> dict[str, dict[str, Callable]]:
    """Arm families; each maps a parameter label to its arm."""
    return {
        "staan-first": {"": staan_first},
        "learned": {"": learned},
        "slot": {f"slot {p}": slot(p) for p in SLOTS},
        "merge": {f"merge {d}": merge(d) for d in MARGINS},
        "merge-rest": {f"merge-rest {d}": merge(d, rest=True) for d in MARGINS},
        "merge, protect": {f"merge {d}, protect {k}": merge(d, protect=k) for d in MARGINS for k in PROTECT},
        "merge, cap": {f"merge {d}, cap {m}": merge(d, cap=m) for d in MARGINS for m in CAPS},
        "optimal merge (regression)": {
            f"optimal merge (regression) {d}": optimal_merge("predicted", d) for d in MARGINS
        },
        "oracle merge": {"": optimal_merge("grade")},
        "oracle": {"": lambda candidates: sorted(candidates, key=lambda c: -c["grade"])},
    }


def report(path: Path = OOF, families: dict[str, dict[str, Callable]] | None = None, show_all: bool = True):
    data = {q: v for q, v in json.loads(path.read_text()).items() if sum(c["grade"] for c in v["candidates"])}
    queries = sorted(data)
    folds = np.array([data[q]["fold"] for q in queries])
    staan_counts = [sum(c["staan_rank"] is not None for c in data[q]["candidates"]) for q in queries]
    print(
        f"{len(queries)} queries; Staan results a query: median {np.median(staan_counts):.0f}, "
        f"fewer than 10 in {np.mean(np.array(staan_counts) < 10):.0%}"
    )

    scores: dict[str, np.ndarray] = {}
    inserted: dict[str, float] = {}
    families = families or grids()
    for family in families.values():
        for label, arm in family.items():
            name = label or next(k for k, v in families.items() if v is family)
            rankings = [arm(data[q]["candidates"]) for q in queries]
            scores[name] = np.array([ndcg(data[q]["candidates"], r) for q, r in zip(queries, rankings)])
            inserted[name] = float(np.mean([index_in_top10(r) for r in rankings]))

    tuned: dict[str, np.ndarray] = {}
    for family_name, family in families.items():
        if len(family) < 2:
            continue
        values = np.empty(len(queries))
        picks = []
        for fold in range(FOLDS):
            inner = folds != fold
            best = max(family, key=lambda label: scores[label][inner].mean())
            picks.append(best)
            values[~inner] = scores[best][~inner]
        tuned[f"{family_name} (tuned)"] = values
        print(f"{family_name} (tuned) picks: {', '.join(picks)}")

    base = scores["staan-first"]
    rng = np.random.default_rng(0)
    samples = rng.integers(0, len(base), (2000, len(base)))
    print("\n| Arm | NDCG@10 | vs staan-first, 95% CI | index in top 10 |\n|---|---|---|---|")
    shown_arms = (
        {**scores, **tuned}
        if show_all
        else {
            name: values for name, values in {**scores, **tuned}.items() if name in families or name.endswith("(tuned)")
        }
    )
    for name, values in shown_arms.items():
        diff = values - base
        means = diff[samples].mean(axis=1)
        shown = f"{inserted[name]:.2f}" if name in inserted else ""
        print(
            f"| {name} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
            f"{np.percentile(means, 97.5):+.4f}] | {shown} |"
        )


if __name__ == "__main__":
    commands = {
        "oof": oof,
        "report": report,
        "minilm-engb": minilm_engb,
        "judge-oof": judge_oof,
        "judge-report": lambda: report(JUDGE_OOF, judge_grids(), show_all=False),
    }
    commands[sys.argv[1]]()
