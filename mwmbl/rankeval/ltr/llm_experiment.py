"""
Compare LTR models trained on LLM-judged labels against the extension/Google gold dataset.

The LLM dataset (learning-to-rank-llm.csv.gz) carries graded Haiku judgments
(`overall`, 0-10) for a subset of queries; the extension dataset's only signal is
SERP presence (`gold_standard_rank`). This experiment holds out a fixed
query-level test split of the LLM dataset and compares, on that split:

- baseline: an existing model artifact (e.g. the deployed model-current.xgb)
- ext-only: freshly trained on the extension dataset (leakage-filtered)
- llm-only: trained on the LLM train split (label = overall >= threshold)
- mixed:    LLM train split + extension rows, with the extension side
            down-weighted by a true per-row sample weight (--ext-weight);
            --ext-downsample keeps the older stochastic-downsampling behaviour
            for comparison.

Any mode can additionally mix in human curation data (--add-curation): the
judgments_export preference pairs and votes converted to weighted pointwise
rows (see load_curation_frame). Training always uses RustXGBPipeline so any
winning model is the exact artifact that ships (Python XGB feature extraction
is a parallel implementation, not value-verified against Rust).

Every run reports two axes (unless --test-size 0):
- Haiku axis: NDCG (full and @10) with the raw `overall` grades as gains plus
  precision@5/@10 at binary relevance overall >= 4, on the held-out LLM split.
- Human axis: pair-accuracy on a held-out query split of the judgments_export
  preference pairs (ties scored 0.5). The two label sources disagree — models
  trained on one regress on the other — so both numbers gate a retrain.

Training frames exclude BOTH held-out query sets, so numbers are clean on both
axes (this makes Haiku figures a shade lower than the original single-axis runs).

Usage:
    uv run python -m mwmbl.rankeval.ltr.llm_experiment --mode baseline --note baseline
    uv run python -m mwmbl.rankeval.ltr.llm_experiment --mode ext-only --note "A'"
    uv run python -m mwmbl.rankeval.ltr.llm_experiment --mode llm-only --overall-threshold 4 --scale-pos-weight 1.0 --note B
    uv run python -m mwmbl.rankeval.ltr.llm_experiment --mode mixed --ext-weight 0.25 --overall-threshold 4 --scale-pos-weight 1.0 --note C
"""

import gzip
import json
from argparse import ArgumentParser
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import sem
from sklearn.metrics import ndcg_score

from mwmbl.rankeval.paths import (
    CURRENT_MODEL_PATH,
    LEARNING_TO_RANK_DATASET_PATH,
    LEARNING_TO_RANK_LLM_DATASET_PATH,
    ROOT_DIR,
)
from mwmbl.tinysearchengine.ltr import RustXGBPipeline

FEATURE_COLUMNS = ["query", "url", "title", "extract", "score"]
RELEVANT_OVERALL = 4
JUDGMENTS_EXPORT_DIR = ROOT_DIR / "devdata" / "judgments_export"
CURATION_WEIGHT_CAP = 3.0  # bound on accumulated per-(query,url) evidence weight


def _normalise(dataset: pd.DataFrame) -> pd.DataFrame:
    dataset = dataset.copy()
    dataset["qnorm"] = dataset["query"].str.lower().str.strip()
    dataset["title"] = dataset["title"].fillna("")
    dataset["extract"] = dataset["extract"].fillna("")
    dataset["score"] = dataset["score"].fillna(0.0)
    return dataset


def load_datasets() -> tuple[pd.DataFrame, pd.DataFrame]:
    ext = _normalise(pd.read_csv(LEARNING_TO_RANK_DATASET_PATH, lineterminator="\n"))
    llm = _normalise(pd.read_csv(LEARNING_TO_RANK_LLM_DATASET_PATH, lineterminator="\n"))
    return ext, llm


def split_llm_queries(llm: pd.DataFrame, test_size: float, split_seed: int) -> tuple[set[str], set[str]]:
    queries = np.sort(llm["qnorm"].unique())
    rng = np.random.default_rng(split_seed)
    rng.shuffle(queries)
    num_test = int(round(len(queries) * test_size))
    return set(queries[num_test:]), set(queries[:num_test])


def load_curation_frame(weak_neg_weight: float) -> pd.DataFrame:
    """Convert the judgments_export preference pairs and votes to pointwise rows.

    Evidence per (query, url):
    - the pos side of any pair, an upvote, or a validate: label 1, weight 1.0
    - the neg side of a delete pair or a downvote: label 0, weight 1.0
    - the neg side of an add/move/approve pair: label 0, weight `weak_neg_weight`
      (these results were merely out-ranked by the user's edit, not judged bad)

    Rows are aggregated per (query, url): the label is the weighted majority and
    the weight is the accumulated evidence, capped at CURATION_WEIGHT_CAP so
    frequently-curated results don't dominate training. Rows with no title or
    extract are dropped; `score` (the index score feature) is unknown for
    curation rows and set to 0.0.
    """

    def read_jsonl_gz(path: Path) -> list[dict]:
        with gzip.open(path, "rt") as f:
            return [json.loads(line) for line in f]

    evidence: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    texts: dict[tuple[str, str], tuple[str, str, str]] = {}

    def add(query: str, doc: dict, label: float, weight: float) -> None:
        url = doc.get("url")
        title = (doc.get("title") or "").strip()
        extract = (doc.get("extract") or "").strip()
        if not url or not (title or extract):
            return
        qnorm = query.lower().strip()
        evidence[(qnorm, url)].append((label, weight))
        texts.setdefault((qnorm, url), (query, title, extract))

    for pair in read_jsonl_gz(JUDGMENTS_EXPORT_DIR / "pairs.jsonl.gz"):
        add(pair["query"], pair["pos"], 1.0, 1.0)
        neg_weight = 1.0 if pair["rule"] == "delete" else weak_neg_weight
        add(pair["query"], pair["neg"], 0.0, neg_weight)
    for point in read_jsonl_gz(JUDGMENTS_EXPORT_DIR / "pointwise.jsonl.gz"):
        add(point["query"], point, 1.0 if point["label"] > 0 else 0.0, 1.0)

    rows = []
    for (qnorm, url), points in evidence.items():
        total = sum(w for _, w in points)
        positive = sum(w for label, w in points if label > 0)
        query, title, extract = texts[(qnorm, url)]
        rows.append(
            {
                "query": query,
                "qnorm": qnorm,
                "url": url,
                "title": title,
                "extract": extract,
                "score": 0.0,
                "label": 1.0 if positive / total >= 0.5 else 0.0,
                "weight": min(total, CURATION_WEIGHT_CAP),
            }
        )
    return pd.DataFrame(rows)


def load_human_pairs() -> list[dict]:
    """Human preference pairs with text on both sides, plus a qnorm key."""
    with gzip.open(JUDGMENTS_EXPORT_DIR / "pairs.jsonl.gz", "rt") as f:
        pairs = [json.loads(line) for line in f]
    pairs = [p for p in pairs if all(p[s]["title"] and p[s]["extract"] for s in ("pos", "neg"))]
    for pair in pairs:
        pair["qnorm"] = pair["query"].lower().strip()
    return pairs


def split_human_pairs(pairs: list[dict], test_size: float, split_seed: int) -> tuple[list[dict], set[str]]:
    """Query-level test split of the human pairs: (test pairs, test queries)."""
    if test_size <= 0:
        return [], set()
    queries = np.sort(pd.unique([p["qnorm"] for p in pairs]))
    rng = np.random.default_rng(split_seed)
    rng.shuffle(queries)
    test_queries = set(queries[: int(round(len(queries) * test_size))])
    return [p for p in pairs if p["qnorm"] in test_queries], test_queries


def pair_accuracy(model, test_pairs: list[dict]) -> tuple[float, float]:
    """Fraction of held-out human pairs ranked pos > neg (ties count 0.5)."""

    def rec(pair, side):
        return {
            "query": pair["query"],
            "url": pair[side]["url"],
            "title": pair[side]["title"],
            "extract": pair[side]["extract"],
            "score": 0.0,
        }

    pos = np.array(model.predict([rec(p, "pos") for p in test_pairs]))
    neg = np.array(model.predict([rec(p, "neg") for p in test_pairs]))
    correct = np.where(pos > neg, 1.0, np.where(pos == neg, 0.5, 0.0))
    return float(correct.mean()), float((pos == neg).mean())


def build_training_frame(
    mode: str,
    ext: pd.DataFrame,
    llm: pd.DataFrame,
    train_queries: set[str],
    test_queries: set[str],
    overall_threshold: float,
    ext_weight: float,
    ext_downsample: float | None,
    seed: int,
    curation: pd.DataFrame | None = None,
    curation_weight: float = 1.0,
) -> pd.DataFrame:
    llm_train = llm[llm["qnorm"].isin(train_queries)].copy()
    llm_train["label"] = (llm_train["overall"] >= overall_threshold).astype(float)
    llm_train["weight"] = 1.0

    ext_train = ext[~ext["qnorm"].isin(test_queries)].copy()
    ext_train["label"] = ext_train["gold_standard_rank"].notna().astype(float)
    ext_train["weight"] = 1.0

    if mode == "llm-only":
        train = llm_train
    elif mode == "ext-only":
        train = ext_train
    elif mode == "mixed":
        # Graded LLM labels supersede SERP presence for pairs both datasets cover.
        llm_pairs = set(zip(llm_train["qnorm"], llm_train["url"]))
        pair_mask = [pair not in llm_pairs for pair in zip(ext_train["qnorm"], ext_train["url"])]
        ext_train = ext_train[pair_mask]
        if ext_downsample is not None:
            ext_train = ext_train.sample(frac=ext_downsample, random_state=seed)
        else:
            ext_train["weight"] = ext_weight
        train = pd.concat([llm_train, ext_train], ignore_index=True)
    else:
        raise ValueError(f"unknown training mode: {mode}")

    if curation is not None:
        curation = curation[~curation["qnorm"].isin(test_queries)].copy()
        # On (query,url) collisions keep the existing row: the LLM's graded
        # judgment (or SERP gold) already covers that pair; curation adds the
        # pairs no other dataset has. Collisions are counted for visibility.
        train_pairs = set(zip(train["qnorm"], train["url"]))
        collisions = [pair in train_pairs for pair in zip(curation["qnorm"], curation["url"])]
        print(f"Curation: {len(curation)} rows, dropping {sum(collisions)} colliding with training frame")
        curation = curation[[not c for c in collisions]].copy()
        curation["weight"] *= curation_weight
        train = pd.concat([train, curation[FEATURE_COLUMNS + ["qnorm", "label", "weight"]]], ignore_index=True)

    assert not set(train["qnorm"]) & test_queries, "test queries leaked into training frame"
    return train[FEATURE_COLUMNS + ["label", "weight"]]


def train(train_df: pd.DataFrame, scale_pos_weight: float, reg_lambda: float, num_rounds: int) -> RustXGBPipeline:
    # Labels are already binary; threshold=0.5 makes the Rust-side binarisation a no-op.
    model = RustXGBPipeline(
        threshold=0.5,
        scale_pos_weight=scale_pos_weight,
        reg_lambda=reg_lambda,
        num_rounds=num_rounds,
    )
    weights = train_df["weight"] if (train_df["weight"] != 1.0).any() else None
    model.fit(train_df[FEATURE_COLUMNS], train_df["label"], sample_weight=weights)
    return model


def evaluate(model, test_df: pd.DataFrame) -> dict:
    test_df = test_df.copy()
    test_df["prediction"] = model.predict(test_df[FEATURE_COLUMNS])

    ndcgs = []
    ndcgs_at_10 = []
    precisions = {5: [], 10: []}
    skipped = 0
    for _, rankings in test_df.groupby("qnorm"):
        gains = rankings["overall"].tolist()
        predictions = rankings["prediction"].tolist()
        if len(rankings) == 1 or sum(gains) == 0:
            skipped += 1
            continue
        ndcgs.append(ndcg_score([gains], [predictions]))
        ndcgs_at_10.append(ndcg_score([gains], [predictions], k=10))

        ranked = rankings.sort_values("prediction", ascending=False)
        relevant = ranked["overall"] >= RELEVANT_OVERALL
        for k in precisions:
            top = relevant.head(k)
            precisions[k].append(top.sum() / len(top))

    return {
        "ndcg": np.mean(ndcgs),
        "ndcg_sem": sem(ndcgs),
        "ndcg@10": np.mean(ndcgs_at_10),
        "p@5": np.mean(precisions[5]),
        "p@10": np.mean(precisions[10]),
        "queries": len(ndcgs),
        "skipped": skipped,
    }


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["baseline", "ext-only", "llm-only", "mixed"])
    parser.add_argument("--note", required=True)
    parser.add_argument(
        "--model-path", default=str(CURRENT_MODEL_PATH), help="model artifact to evaluate in baseline mode"
    )
    parser.add_argument(
        "--overall-threshold", type=float, default=4.0, help="LLM rows are positive when overall >= this"
    )
    parser.add_argument(
        "--ext-weight", type=float, default=1.0, help="per-row sample weight for extension rows in mixed mode"
    )
    parser.add_argument(
        "--ext-downsample",
        type=float,
        default=None,
        help="legacy behaviour: keep this fraction of extension rows instead of weighting",
    )
    parser.add_argument(
        "--add-curation",
        action="store_true",
        help="mix in the judgments_export curation/vote data as weighted pointwise rows",
    )
    parser.add_argument("--curation-weight", type=float, default=1.0, help="multiplier on curation row weights")
    parser.add_argument(
        "--weak-neg-weight", type=float, default=0.25, help="weight of out-ranked (add/move/approve) pair negatives"
    )
    parser.add_argument("--scale-pos-weight", type=float, default=0.1)
    parser.add_argument("--reg-lambda", type=float, default=2.0)
    parser.add_argument("--num-rounds", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0, help="downsampling seed (--ext-downsample only)")
    parser.add_argument(
        "--split-seed", type=int, default=42, help="train/test query split seed; keep fixed across compared runs"
    )
    parser.add_argument("--test-size", type=float, default=0.3)
    parser.add_argument("--save-model", default=None, help="optional path to save the trained model")
    args = parser.parse_args()

    ext, llm = load_datasets()
    train_queries, test_queries = split_llm_queries(llm, args.test_size, args.split_seed)
    test_df = llm[llm["qnorm"].isin(test_queries)]
    human_test_pairs, human_test_queries = split_human_pairs(load_human_pairs(), args.test_size, args.split_seed)
    print(
        f"LLM split: {len(train_queries)} train / {len(test_queries)} test queries "
        f"({len(test_df)} test rows); {len(human_test_pairs)} held-out human pairs "
        f"({len(human_test_queries)} queries)"
    )

    if args.mode == "baseline":
        print(f"Loading model from {args.model_path}")
        model = RustXGBPipeline.from_model_path(args.model_path)
    else:
        curation = load_curation_frame(args.weak_neg_weight) if args.add_curation else None
        train_df = build_training_frame(
            args.mode,
            ext,
            llm,
            train_queries - human_test_queries,
            test_queries | human_test_queries,
            args.overall_threshold,
            args.ext_weight,
            args.ext_downsample,
            args.seed,
            curation=curation,
            curation_weight=args.curation_weight,
        )
        print(
            f"Training frame: {len(train_df)} rows, "
            f"positive rate {train_df['label'].mean():.3f}, "
            f"weighted positive rate {np.average(train_df['label'], weights=train_df['weight']):.3f}"
        )
        model = train(train_df, args.scale_pos_weight, args.reg_lambda, args.num_rounds)
        if args.save_model:
            print(f"Saving model to {args.save_model}")
            model.save_model(args.save_model)

    if test_df.empty:
        print("Empty test split (--test-size 0): trained on everything, skipping evaluation")
        return

    metrics = evaluate(model, test_df)
    accuracy, ties = pair_accuracy(model, human_test_pairs)
    print(f"\n=== {args.note} (mode={args.mode}) ===")
    print(f"queries evaluated: {metrics['queries']} (skipped {metrics['skipped']})")
    print(f"ndcg:    {metrics['ndcg']:.4f} ± {metrics['ndcg_sem']:.4f}")
    print(f"ndcg@10: {metrics['ndcg@10']:.4f}")
    print(f"p@5:     {metrics['p@5']:.4f}")
    print(f"p@10:    {metrics['p@10']:.4f}")
    print(f"pair-acc: {accuracy:.3f} on {len(human_test_pairs)} held-out human pairs (ties {ties:.1%})")


if __name__ == "__main__":
    run()
