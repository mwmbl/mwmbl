"""
Does a logistic regression on the item text add anything to the LTR features?

The LR is trained on TF-IDF of ``title + extract`` to predict LLM relevance
(``overall >= threshold``). Its probability is appended to the Rust feature matrix
(``RustXGBPipeline.extract_features``) as one extra column for an XGBoost classifier.

Leakage control: a row's LR feature must never come from an LR that saw its own label, or
XGBoost learns to trust it too much. So within every outer fold the training rows get
out-of-fold LR scores (inner GroupKFold), and the test rows get scores from an LR fit on
the whole training fold. Everything is grouped by query, so no query spans train and test.

Reports, per arm, the same Haiku-axis metrics as ``llm_experiment``:
``xgb`` (Rust features only), ``xgb+lr`` (plus the LR column) and ``lr`` (LR alone).

Usage::

    uv run python -m mwmbl.rankeval.ltr.text_lr_experiment --folds 5
"""

from argparse import ArgumentParser

import numpy as np
import pandas as pd
from scipy.stats import sem
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

from mwmbl.rankeval.ltr.llm_experiment import FEATURE_COLUMNS, RELEVANT_OVERALL, evaluate, load_datasets
from mwmbl_rank import RustXGBPipeline as RustFeatures


class PrecomputedPredictions:
    """Adapts saved predictions to the ``model.predict(frame)`` interface ``evaluate`` expects."""

    def __init__(self, predictions: np.ndarray):
        self.predictions = predictions

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return self.predictions


def item_text(frame: pd.DataFrame) -> pd.Series:
    return frame["title"] + " " + frame["extract"]


def make_text_model(c: float, max_features: int) -> tuple[TfidfVectorizer, LogisticRegression]:
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2), min_df=2, max_features=max_features, sublinear_tf=True, strip_accents="unicode"
    )
    return vectorizer, LogisticRegression(C=c, max_iter=1000)


def fit_text_model(train: pd.DataFrame, c: float, max_features: int):
    vectorizer, classifier = make_text_model(c, max_features)
    classifier.fit(vectorizer.fit_transform(item_text(train)), train["label"])
    return vectorizer, classifier


def predict_text_model(fitted, frame: pd.DataFrame) -> np.ndarray:
    vectorizer, classifier = fitted
    return classifier.predict_proba(vectorizer.transform(item_text(frame)))[:, 1]


def out_of_fold_text_scores(train: pd.DataFrame, c: float, max_features: int, folds: int) -> np.ndarray:
    scores = np.zeros(len(train))
    for fit_index, score_index in GroupKFold(n_splits=folds).split(train, groups=train["qnorm"]):
        fitted = fit_text_model(train.iloc[fit_index], c, max_features)
        scores[score_index] = predict_text_model(fitted, train.iloc[score_index])
    return scores


def rust_features(frame: pd.DataFrame) -> np.ndarray:
    records = frame[FEATURE_COLUMNS].to_dict("records")
    return np.array(RustFeatures.extract_features(records), dtype=np.float32)


def fit_xgb(features: np.ndarray, labels: pd.Series) -> XGBClassifier:
    return XGBClassifier(n_estimators=100, reg_lambda=2, scale_pos_weight=1.0, n_jobs=4).fit(features, labels)


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--inner-folds", type=int, default=5)
    parser.add_argument("--overall-threshold", type=float, default=RELEVANT_OVERALL)
    parser.add_argument("--c", type=float, default=1.0, help="LR inverse regularisation strength")
    parser.add_argument("--max-features", type=int, default=50000)
    args = parser.parse_args()

    _, llm = load_datasets()
    llm = llm.reset_index(drop=True)
    llm["label"] = (llm["overall"] >= args.overall_threshold).astype(float)
    base_features = rust_features(llm)
    print(f"{len(llm)} rows, {llm['qnorm'].nunique()} queries, positive rate {llm['label'].mean():.3f}")

    arms = ["xgb", "xgb+lr", "lr"]
    results = {arm: [] for arm in arms}
    for fold, (train_index, test_index) in enumerate(GroupKFold(n_splits=args.folds).split(llm, groups=llm["qnorm"])):
        train, test = llm.iloc[train_index], llm.iloc[test_index]

        train_lr = out_of_fold_text_scores(train, args.c, args.max_features, args.inner_folds)
        test_lr = predict_text_model(fit_text_model(train, args.c, args.max_features), test)

        predictions = {
            "xgb": fit_xgb(base_features[train_index], train["label"]).predict_proba(base_features[test_index])[:, 1],
            "xgb+lr": fit_xgb(np.column_stack([base_features[train_index], train_lr]), train["label"]).predict_proba(
                np.column_stack([base_features[test_index], test_lr])
            )[:, 1],
            "lr": test_lr,
        }
        for arm in arms:
            metrics = evaluate(PrecomputedPredictions(predictions[arm]), test)
            results[arm].append(metrics)
        line = ", ".join(f"{arm} {results[arm][-1]['ndcg']:.4f}" for arm in arms)
        print(f"fold {fold + 1}/{args.folds}: ndcg {line}")

    print("\n=== mean over folds ===")
    for arm in arms:
        per_fold = pd.DataFrame(results[arm])
        ndcg = per_fold["ndcg"]
        print(
            f"{arm:8s} ndcg {ndcg.mean():.4f} ± {sem(ndcg):.4f}  ndcg@10 {per_fold['ndcg@10'].mean():.4f}  "
            f"p@5 {per_fold['p@5'].mean():.4f}  p@10 {per_fold['p@10'].mean():.4f}"
        )
    gains = pd.DataFrame(results["xgb+lr"])["ndcg"] - pd.DataFrame(results["xgb"])["ndcg"]
    print(f"\nxgb+lr minus xgb, ndcg per fold: {np.round(gains.values, 4).tolist()} (mean {gains.mean():+.4f})")


if __name__ == "__main__":
    run()
