"""Binary logistic vs ranking objectives for the Combined Search model, with and without the
serving-pool labels, under 5-fold cross-validation over the 849 LLM-labelled queries.

Every arm is trained in Python XGBoost on the same features, extracted by mwmbl_rank (the
50 shared features plus `in_staan` and `staan_rank`), with the tree parameters the shipped
model uses. The arms differ only in objective and training data:

- `binary`: the shipped recipe. LLM rows are positive when overall >= 4; the extension
  dataset's rows are positive when they are in the gold SERP, at weight 0.25.
- `ndcg` / `pairwise`: `rank:ndcg` / `rank:pairwise` on the graded overall score (linear
  gain), with the extension rows as groups of their own at weight 0.25, gold = 7.
- `+new`: adds the 8,082 serving-pool labels of `pass3_serving_pool.jsonl`, each moved back
  onto the original scale by its judge's mean anchor drift.

Each held-out fold is scored on two populations: the original Pass-2 pool (what
`llm_experiment` evaluates on), and the serving pool of `pool.json` (Staan's results plus
the index's top 30, which is what the model ranks in production). The majority-terms
filter drops a candidate outright in every arm. It must not be applied as a score of 0, as
`LTRRanker` and the Python pipelines do: a ranking objective's scores go negative, so a
filtered document scored 0 would rank above real ones.

The curation data (`devdata/judgments_export/`) and the exact Staan ranks
(`pass2_staan.jsonl`) aren't available here, so no arm uses curation. The Staan ranks of
the training rows are reconstructed: see `staan_ranks`.

Writes `devdata/combined_ltr_labels/objective_experiment.json` with per-query scores.
"""

import json
import os
from pathlib import Path

import django
import numpy as np
import pandas as pd
import xgboost as xgb

import mwmbl_rank

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE

LLM_DATASET = Path("devdata/rankeval-2026-04/learning-to-rank-llm.csv.gz")
EXT_DATASET = Path("devdata/rankeval-2026-04/learning-to-rank.csv.gz")
LABELS = Path("devdata/combined_ltr_labels")
OUT = LABELS / "objective_experiment.json"

OVERALL_THRESHOLD = 4
EXT_WEIGHT = 0.25
EXT_GOLD_OVERALL = 7
# Shrinks a judge's mean anchor drift towards zero: about 30 anchors a judge put the
# standard error of that mean near 0.4.
DRIFT_SHRINKAGE = 10
FOLDS = 5
NUM_ROUNDS = 100
TREE_PARAMS = {"tree_method": "exact", "lambda": 2.0, "eta": 0.3, "max_depth": 6, "nthread": 4}
OBJECTIVES = {
    "binary": {"objective": "binary:logistic", "scale_pos_weight": 1.0},
    "ndcg": {"objective": "rank:ndcg", "ndcg_exp_gain": False},
    "pairwise": {"objective": "rank:pairwise"},
}
FEATURE_NAMES = list(mwmbl_rank.FEATURE_NAMES) + list(mwmbl_rank.PROVIDER_FEATURE_NAMES)
NUM_TERMS = FEATURE_NAMES.index("num_terms")
MATCH_TERMS = FEATURE_NAMES.index("match_terms")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in open(path)]


def staan_ranks(llm: pd.DataFrame) -> dict[tuple[str, str], int]:
    """(query, url) -> Staan's 0-based rank, for the LLM dataset's Staan rows.

    A row Staan pooled first carries staan_score(rank) as its score, so its rank is exact.
    A row another pool had already added keeps that pool's score. Those rows take the ranks
    left over in their query, ordered by Google's rank: Staan's order follows Google's
    closely, but this part is an approximation.
    """
    ranks = {}
    for query, rows in llm[llm["pools"].str.contains("staan")].groupby("query"):
        first = rows[rows["pools"].str.startswith("staan")]
        known = {url: round(STAAN_TOP_SCORE - score) for url, score in zip(first["url"], first["score"])}
        rest = rows[~rows["pools"].str.startswith("staan")].sort_values("gold_standard_rank", na_position="last")
        free = [rank for rank in range(len(rows)) if rank not in known.values()]
        ranks.update({(query, url): rank for url, rank in known.items()})
        ranks.update({(query, url): rank for url, rank in zip(rest["url"], free)})
    return ranks


def judge_drift() -> dict[str, float]:
    original = {
        (j["query"], j["url"]): j["overall"] for j in read_jsonl(Path("devdata/llm_relabel/pass3_judgments.jsonl"))
    }
    drifts: dict[str, list[int]] = {}
    for row in read_jsonl(LABELS / "pass3_serving_pool.jsonl"):
        if row.get("anchor"):
            drifts.setdefault(row["judge"], []).append(row["overall"] - original[(row["query"], row["url"])])
    return {judge: sum(values) / (len(values) + DRIFT_SHRINKAGE) for judge, values in drifts.items()}


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The LLM, extension and new training rows, and the serving pool to evaluate on."""
    llm = pd.read_csv(LLM_DATASET, lineterminator="\n")
    ranks = staan_ranks(llm)
    llm["staan_asked"] = True
    llm["staan_rank"] = [ranks.get(pair) for pair in zip(llm["query"], llm["url"])]
    llm["from_staan"] = llm["staan_rank"].notna()
    llm["source"] = "llm"

    ext = pd.read_csv(EXT_DATASET, lineterminator="\n")
    ext["overall"] = np.where(ext["gold_standard_rank"].notna(), EXT_GOLD_OVERALL, 0)
    ext["staan_asked"] = False
    ext["staan_rank"] = None
    ext["from_staan"] = False
    ext["source"] = "ext"

    pool = json.loads((LABELS / "pool.json").read_text())
    drift = judge_drift()
    # The top-up judge graded 8 skipped candidates and no anchors, so it has no drift to remove.
    new_grades = {
        (row["query"], row["url"]): int(np.clip(round(row["overall"] - drift.get(row["judge"], 0.0)), 0, 10))
        for row in read_jsonl(LABELS / "pass3_serving_pool.jsonl")
        if not row.get("anchor")
    }
    old_grades = dict(zip(zip(llm["query"], llm["url"]), llm["overall"]))

    serving_rows = []
    for query, rows in pool.items():
        for row in rows:
            pair = (query, row["url"])
            staan = row["source"] != "index"
            rank = ranks.get(pair) if staan else None
            serving_rows.append(
                {
                    "query": query,
                    "url": row["url"],
                    "title": row["title"] or "",
                    "extract": row["extract"] or "",
                    # What serving scores a Staan result with; the index's own score otherwise.
                    "score": STAAN_TOP_SCORE - rank if rank is not None else (row["score"] or 0.0),
                    "staan_asked": True,
                    "staan_rank": rank,
                    "from_staan": staan,
                    "overall": new_grades[pair] if pair in new_grades else old_grades[pair],
                    "new": pair in new_grades,
                }
            )
    serving = pd.DataFrame(serving_rows)
    new = serving[serving["new"]].copy()
    new["source"] = "new"
    return llm, ext, new, serving


def features(frame: pd.DataFrame) -> np.ndarray:
    columns = ["query", "url", "title", "extract", "score", "staan_asked", "staan_rank", "from_staan"]
    records = frame[columns].copy()
    records["title"] = records["title"].fillna("")
    records["extract"] = records["extract"].fillna("")
    records["score"] = records["score"].fillna(0.0)
    records["staan_rank"] = records["staan_rank"].astype(object).where(records["staan_rank"].notna(), None)
    return np.array(mwmbl_rank.RustXGBPipeline.extract_features(records.to_dict("records"), True), dtype=np.float32)


def train(objective: str, frame: pd.DataFrame, feats: np.ndarray) -> xgb.Booster:
    params = {**TREE_PARAMS, **OBJECTIVES[objective]}
    if objective == "binary":
        labels = (frame["overall"] >= OVERALL_THRESHOLD).astype(float)
        weights = np.where(frame["source"] == "ext", EXT_WEIGHT, 1.0)
        dmatrix = xgb.DMatrix(feats, label=labels, weight=weights)
    else:
        # Ranking groups must be contiguous; extension queries are groups of their own.
        group_key = (
            frame["source"].eq("ext").map({True: "ext:", False: "llm:"}) + frame["query"].str.lower().str.strip()
        )
        order = np.argsort(group_key.to_numpy(), kind="stable")
        keys = group_key.to_numpy()[order]
        _, qid = np.unique(keys, return_inverse=True)
        group_weights = np.array([EXT_WEIGHT if key.startswith("ext:") else 1.0 for key in dict.fromkeys(keys)])
        dmatrix = xgb.DMatrix(feats[order], label=frame["overall"].to_numpy()[order], qid=qid, weight=group_weights)
    return xgb.train(params, dmatrix, NUM_ROUNDS)


def kept(frame: pd.DataFrame, feats: np.ndarray) -> np.ndarray:
    """The candidates the majority-terms filter keeps: Staan's results are exempt."""
    return frame["from_staan"].to_numpy() | (feats[:, MATCH_TERMS] > feats[:, NUM_TERMS] / 2)


def ndcg_at_10(gains: np.ndarray, scores: np.ndarray, keep: np.ndarray) -> float:
    discounts = 1 / np.log2(np.arange(2, 12))
    ideal = np.sort(gains)[::-1][:10]
    ideal_dcg = float(np.sum(ideal * discounts[: len(ideal)]))
    ranked = gains[keep][np.argsort(-scores[keep], kind="stable")][:10]
    return float(np.sum(ranked * discounts[: len(ranked)])) / ideal_dcg if ideal_dcg else float("nan")


def score_population(booster: xgb.Booster, frame: pd.DataFrame, feats: np.ndarray) -> dict[str, dict]:
    scores = booster.predict(xgb.DMatrix(feats))
    keep = kept(frame, feats)
    result = {}
    for query, index in frame.groupby("query").indices.items():
        gains = frame["overall"].to_numpy()[index].astype(float)
        order = index[keep[index]][np.argsort(-scores[index][keep[index]], kind="stable")][:10]
        top = frame["overall"].to_numpy()[order]
        result[query] = {
            "ndcg@10": ndcg_at_10(gains, scores[index], keep[index]),
            "weak_in_top10": float(np.mean(top <= 3)) if len(top) else float("nan"),
            "index_in_top10": float(np.mean(~frame["from_staan"].to_numpy()[order])) if len(top) else float("nan"),
        }
    return result


def run():
    llm, ext, new, serving = load()
    print(f"llm {len(llm)}, ext {len(ext)}, new {len(new)}, serving pool {len(serving)} rows")
    feats = {name: features(frame) for name, frame in [("llm", llm), ("ext", ext), ("new", new), ("serving", serving)]}

    queries = np.array(sorted(llm["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    folds = np.array_split(queries, FOLDS)
    arms = [(objective, with_new) for objective in OBJECTIVES for with_new in (False, True)]
    results: dict[str, dict[str, dict]] = {
        f"{o}{'+new' if w else ''}": {"original": {}, "serving": {}} for o, w in arms
    }

    for fold, test_queries in enumerate(folds):
        test = set(test_queries)
        test_norm = {q.lower().strip() for q in test}
        llm_train = ~llm["query"].isin(test)
        ext_train = ~ext["query"].str.lower().str.strip().isin(test_norm)
        new_train = ~new["query"].isin(test)
        llm_test = llm["query"].isin(test).to_numpy()
        serving_test = serving["query"].isin(test).to_numpy()
        for objective, with_new in arms:
            parts = [
                (llm[llm_train], feats["llm"][llm_train.to_numpy()]),
                (ext[ext_train], feats["ext"][ext_train.to_numpy()]),
            ]
            if with_new:
                parts.append((new[new_train], feats["new"][new_train.to_numpy()]))
            frame = pd.concat([p[0] for p in parts], ignore_index=True)
            booster = train(objective, frame, np.concatenate([p[1] for p in parts]))
            arm = f"{objective}{'+new' if with_new else ''}"
            results[arm]["original"].update(
                score_population(booster, llm[llm_test].reset_index(drop=True), feats["llm"][llm_test])
            )
            results[arm]["serving"].update(
                score_population(booster, serving[serving_test].reset_index(drop=True), feats["serving"][serving_test])
            )
            print(f"fold {fold} {arm} done", flush=True)

    OUT.write_text(json.dumps(results))
    report(results)


def report(results: dict[str, dict[str, dict]]):
    rng = np.random.default_rng(0)
    base = "binary"
    for population in ("serving", "original"):
        print(f"\n## {population} pool, 5-fold CV over 849 queries\n")
        print("| Arm | NDCG@10 | vs binary, 95% CI | weak in top 10 | index in top 10 |")
        print("|---|---|---|---|---|")
        queries = sorted(q for q, v in results[base][population].items() if not np.isnan(v["ndcg@10"]))
        baseline = np.array([results[base][population][q]["ndcg@10"] for q in queries])
        for arm, scores in results.items():
            values = np.array([scores[population][q]["ndcg@10"] for q in queries])
            diff = values - baseline
            means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
            weak = np.nanmean([scores[population][q]["weak_in_top10"] for q in queries])
            index = np.nanmean([scores[population][q]["index_in_top10"] for q in queries])
            print(
                f"| {arm} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
                f"{np.percentile(means, 97.5):+.4f}] | {weak:.1%} | {index:.1%} |"
            )


if __name__ == "__main__":
    run()
