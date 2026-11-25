"""Can the `wiki_score` LTR feature go? (#707)

Cross-validates the two deployed recipes with whatever feature set the installed `mwmbl_rank` build
has, so the two arms are two builds: main's (50 features) and one without `wiki_score` (49). Run each
command once per build with its own TAG, then `compare`.

- `standard`: the recipe behind mwmbl/resources/model.xgb (#626's `images_relevance`: label relevance >= 2,
  scale_pos_weight 0.1, reg_lambda 2, 100 rounds, curation at 0.5, weak negatives at 0.25), less #621's
  Commons image rows, which are not available.
- `combined`: provider_features.py's recipe behind model-combined.xgb (mixed LLM + extension rows,
  overall >= 4, ext 0.25, curation 0.5, scale_pos_weight 1.0), with Staan's ranks rebuilt from the
  Pass-2 pool because pass2_staan.jsonl is not available (see `staan_ranks_from_pool`).

Five folds by query (seed 42), scored by pass-3 NDCG@10 on each held-out fold, and pair accuracy on a
held-out fold of the human curation pairs, as seed_images/ltr_retrain.py does.

    cv RECIPE TAG              -> devdata/wiki_score_ablation/{cv,lists}-RECIPE-TAG.json, the second
                                  each query's held-out top ten for holistic_judge.py
    train RECIPE PATH          a model trained on every query
    compare RECIPE TAG_A TAG_B paired differences, B minus A
"""

import json
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import sem
from sklearn.metrics import ndcg_score

import mwmbl_rank
from mwmbl.rankeval.ltr.llm_experiment import (
    FEATURE_COLUMNS,
    build_training_frame,
    load_curation_frame,
    load_datasets,
    load_human_pairs,
)
from mwmbl.rankeval.paths import ROOT_DIR
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE

OUTPUT_DIR = ROOT_DIR / "devdata" / "wiki_score_ablation"
POOL_PATH = ROOT_DIR / "devdata" / "llm_relabel" / "pass2_pool.jsonl"
NUM_FOLDS = 5
SPLIT_SEED = 42
WEAK_NEGATIVE_WEIGHT = 0.25
CURATION_WEIGHT = 0.5
REG_LAMBDA = 2.0
NUM_ROUNDS = 100
STANDARD_SCALE_POS_WEIGHT = 0.1
STANDARD_RELEVANCE_THRESHOLD = 2
COMBINED_SCALE_POS_WEIGHT = 1.0
COMBINED_OVERALL_THRESHOLD = 4.0
COMBINED_EXT_WEIGHT = 0.25
PROVIDER_COLUMNS = ["staan_asked", "staan_rank"]


def staan_ranks_from_pool() -> dict[str, dict[str, int]]:
    """qnorm -> {url: Staan's 0-based rank}, rebuilt from the Pass-2 pool.

    A result Staan pooled first has score STAAN_TOP_SCORE - rank, so its rank is exact. A result
    another source pooled first kept that source's score, so its rank is lost: those take the ranks
    left free, in pool order (about 7% of Staan's results).
    """
    ranks = {}
    with open(POOL_PATH) as f:
        for line in f:
            record = json.loads(line)
            staan = [candidate for candidate in record["candidates"] if "staan" in candidate["pools"]]
            known = {}
            for candidate in staan:
                score = candidate["score"]
                if candidate["ss_source"] == "staan" and score is not None:
                    rank = STAAN_TOP_SCORE - score
                    if rank >= 0 and rank == round(rank) and round(rank) not in known.values():
                        known[candidate["url"]] = round(rank)
            unknown = [candidate["url"] for candidate in staan if candidate["url"] not in known]
            free = (rank for rank in range(len(staan) + len(known)) if rank not in set(known.values()))
            ranks[record["query"].lower().strip()] = known | dict(zip(unknown, free))
    return ranks


def with_staan(frame: pd.DataFrame, ranks: dict[str, dict[str, int]]) -> pd.DataFrame:
    query_ranks = [ranks.get(query.lower().strip()) for query in frame["query"]]
    return frame.assign(
        staan_asked=[query is not None for query in query_ranks],
        staan_rank=[None if query is None else query.get(url) for url, query in zip(frame["url"], query_ranks)],
    )


def folds(keys: list[str]) -> dict[str, int]:
    shuffled = np.sort(np.unique(keys))
    np.random.default_rng(SPLIT_SEED).shuffle(shuffled)
    return {key: position % NUM_FOLDS for position, key in enumerate(shuffled)}


def standard_frame(llm, curation, excluded: set[str]) -> pd.DataFrame:
    """seed_images/ltr_retrain.py's training_frame for the images_relevance arm, without image rows."""
    rows = llm[~llm["qnorm"].isin(excluded)]
    rows = rows.assign(label=(rows["relevance"] >= STANDARD_RELEVANCE_THRESHOLD).astype(float), weight=1.0)
    covered = set(zip(rows["qnorm"], rows["url"]))
    curation = curation[~curation["qnorm"].isin(excluded)]
    uncovered = np.array([pair not in covered for pair in zip(curation["qnorm"], curation["url"])], dtype=bool)
    curation = curation.loc[uncovered]
    curation = curation.assign(weight=curation["weight"] * CURATION_WEIGHT)
    return pd.concat([rows, curation], ignore_index=True)[FEATURE_COLUMNS + ["label", "weight"]]


def train_model(recipe: str, ext, llm, curation, ranks, excluded: set[str]) -> RustXGBPipeline:
    if recipe == "standard":
        frame = standard_frame(llm, curation, excluded)
        model = RustXGBPipeline(
            threshold=0.5, scale_pos_weight=STANDARD_SCALE_POS_WEIGHT, reg_lambda=REG_LAMBDA, num_rounds=NUM_ROUNDS
        )
        model.fit(frame[FEATURE_COLUMNS], frame["label"], sample_weight=frame["weight"])
        return model
    frame = build_training_frame(
        "mixed",
        ext,
        llm,
        train_queries=set(llm["qnorm"]) - excluded,
        test_queries=excluded,
        overall_threshold=COMBINED_OVERALL_THRESHOLD,
        ext_weight=COMBINED_EXT_WEIGHT,
        ext_downsample=None,
        seed=0,
        curation=curation,
        curation_weight=CURATION_WEIGHT,
    )
    frame = with_staan(frame, ranks)
    model = RustXGBPipeline(
        threshold=0.5,
        scale_pos_weight=COMBINED_SCALE_POS_WEIGHT,
        reg_lambda=REG_LAMBDA,
        num_rounds=NUM_ROUNDS,
        provider_features=True,
    )
    model.fit(frame[FEATURE_COLUMNS + PROVIDER_COLUMNS], frame["label"], sample_weight=frame["weight"])
    return model


def per_query_ndcg(test: pd.DataFrame, gain: str) -> dict[str, float]:
    scores = {}
    for query, rows in test.groupby("qnorm"):
        if len(rows) > 1 and rows[gain].sum() > 0:
            scores[query] = float(ndcg_score([rows[gain].tolist()], [rows["prediction"].tolist()], k=10))
    return scores


def top_ten_lists(test: pd.DataFrame) -> dict[str, list[dict]]:
    """Each held-out query's top ten by the fold's model, as holistic_judge.py's lists files want."""
    lists = {}
    for _, rows in test.groupby("qnorm"):
        ranked = rows.sort_values("prediction", ascending=False, kind="stable").head(10)
        lists[rows["query"].iloc[0]] = ranked[["url", "title", "extract"]].to_dict("records")
    return lists


def pair_record(pair: dict, side: str) -> dict:
    return {"query": pair["query"], **{key: pair[side][key] for key in ("url", "title", "extract")}, "score": 0.0}


def pair_correct(model, pairs: list[dict]) -> list[float]:
    """1 for each human pair ranked pos > neg, 0.5 for a tie. Curation queries were never sent to Staan."""
    pos = np.array(model.predict([pair_record(pair, "pos") for pair in pairs]))
    neg = np.array(model.predict([pair_record(pair, "neg") for pair in pairs]))
    return np.where(pos > neg, 1.0, np.where(pos == neg, 0.5, 0.0)).tolist()


def with_finite_features(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows whose features are all finite: a curation query with no terms (all punctuation or
    full-width spaces) divides by zero, and XGBoost refuses the whole matrix."""
    features = np.array(mwmbl_rank.RustXGBPipeline.extract_features(frame[FEATURE_COLUMNS].to_dict("records")))
    finite = np.isfinite(features).all(axis=1)
    print(f"Dropping {int((~finite).sum())} of {len(frame)} rows with non-finite features")
    return frame[finite]


def with_finite_pairs(pairs: list[dict]) -> list[dict]:
    records = [pair_record(pair, side) for pair in pairs for side in ("pos", "neg")]
    features = np.array(mwmbl_rank.RustXGBPipeline.extract_features(records)).reshape(len(pairs), -1)
    finite = np.isfinite(features).all(axis=1)
    print(f"Dropping {int((~finite).sum())} of {len(pairs)} human pairs with non-finite features")
    return [pair for pair, keep in zip(pairs, finite) if keep]


def load(recipe: str):
    ext, llm = load_datasets()
    curation = with_finite_features(load_curation_frame(WEAK_NEGATIVE_WEIGHT))
    ranks = staan_ranks_from_pool() if recipe == "combined" else {}
    return ext, llm, curation, ranks


def cross_validate(recipe: str, tag: str) -> None:
    ext, llm, curation, ranks = load(recipe)
    human_pairs = with_finite_pairs(load_human_pairs())
    llm_folds = folds(llm["qnorm"].tolist())
    human_folds = folds([pair["qnorm"] for pair in human_pairs])
    ndcg = {"overall": {}, "relevance": {}}
    pairs = []
    lists = {}
    for fold in range(NUM_FOLDS):
        test_queries = {query for query, number in llm_folds.items() if number == fold}
        human_test_queries = {query for query, number in human_folds.items() if number == fold}
        model = train_model(recipe, ext, llm, curation, ranks, test_queries | human_test_queries)
        test = llm[llm["qnorm"].isin(test_queries)]
        columns = FEATURE_COLUMNS
        if recipe == "combined":
            test = with_staan(test, ranks)
            columns = FEATURE_COLUMNS + PROVIDER_COLUMNS
        test = test.assign(prediction=model.predict(test[columns]))
        for gain, scores in ndcg.items():
            scores.update(per_query_ndcg(test, gain))
        lists |= top_ten_lists(test)
        pairs += pair_correct(model, [pair for pair in human_pairs if pair["qnorm"] in human_test_queries])
        print(f"fold {fold}: {np.mean(list(ndcg['overall'].values())):.4f}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = {"num_features": mwmbl_rank.NUM_FEATURES, "ndcg@10": ndcg, "human pairs": pairs}
    (OUTPUT_DIR / f"cv-{recipe}-{tag}.json").write_text(json.dumps(result))
    (OUTPUT_DIR / f"lists-{recipe}-{tag}.json").write_text(json.dumps(lists))
    print(f"{recipe} {tag}: {mwmbl_rank.NUM_FEATURES} features, NDCG@10 {np.mean(list(ndcg['overall'].values())):.4f}")


def compare(recipe: str, tag_a: str, tag_b: str) -> None:
    a, b = (json.loads((OUTPUT_DIR / f"cv-{recipe}-{tag}.json").read_text()) for tag in (tag_a, tag_b))
    print(f"{recipe}: {tag_a} ({a['num_features']} features) vs {tag_b} ({b['num_features']} features)")
    for gain in ("overall", "relevance"):
        shared = sorted(set(a["ndcg@10"][gain]) & set(b["ndcg@10"][gain]))
        before = np.array([a["ndcg@10"][gain][query] for query in shared])
        after = np.array([b["ndcg@10"][gain][query] for query in shared])
        difference = after - before
        print(
            f"  NDCG@10 ({gain} gain), {len(shared)} queries: {before.mean():.4f} -> {after.mean():.4f}, "
            f"diff {difference.mean():+.4f} +/- {sem(difference):.4f} (wins {int((difference > 0).sum())}, "
            f"losses {int((difference < 0).sum())})"
        )
    pairs_a, pairs_b = np.array(a["human pairs"]), np.array(b["human pairs"])
    pair_difference = pairs_b - pairs_a
    print(
        f"  human pair accuracy, {len(pairs_a)} pairs: {pairs_a.mean():.4f} -> {pairs_b.mean():.4f}, "
        f"diff {pair_difference.mean():+.4f} +/- {sem(pair_difference):.4f}"
    )


def run():
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cv = commands.add_parser("cv")
    cv.add_argument("recipe", choices=["standard", "combined"])
    cv.add_argument("tag")
    train = commands.add_parser("train")
    train.add_argument("recipe", choices=["standard", "combined"])
    train.add_argument("path")
    comparison = commands.add_parser("compare")
    comparison.add_argument("recipe", choices=["standard", "combined"])
    comparison.add_argument("tag_a")
    comparison.add_argument("tag_b")
    args = parser.parse_args()
    if args.command == "cv":
        cross_validate(args.recipe, args.tag)
    elif args.command == "train":
        model = train_model(args.recipe, *load(args.recipe), set())
        Path(args.path).parent.mkdir(parents=True, exist_ok=True)
        model.save_model(args.path)
        print(f"Saved {args.path} ({mwmbl_rank.NUM_FEATURES} features)")
    else:
        compare(args.recipe, args.tag_a, args.tag_b)


if __name__ == "__main__":
    run()
