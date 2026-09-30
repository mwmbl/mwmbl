"""
Does a logistic regression on the item text add anything to the LTR features?

The LR is trained on TF-IDF of ``title + extract`` to predict LLM relevance
(``overall >= threshold``). Its probability is appended to the Rust feature matrix
(``RustXGBPipeline.extract_features``) as one extra column for an XGBoost classifier.

Leakage control: a row's LR feature must never come from an LR that saw its own label, or
XGBoost learns to trust it too much. So within every outer fold the training rows get
out-of-fold LR scores (inner GroupKFold), and the test rows get scores from an LR fit on
the whole training fold. Everything is grouped by query, so no query spans train and test.

Two LR variants: ``text`` (item TF-IDF only) and ``cross`` (also hashed query-term x
item-term pairs, per field, so it can learn e.g. that "python" in the query and "tutorial" in
the title go together). Arms: ``xgb`` (Rust features only), ``xgb+text``, ``xgb+cross`` (the LR
score added as a column) and the LRs alone, scored with the ``llm_experiment`` Haiku-axis metrics.

Usage::

    uv run python -m mwmbl.rankeval.ltr.text_lr_experiment --folds 5
"""

import re
from argparse import ArgumentParser

import numpy as np
import pandas as pd
from scipy.sparse import hstack
from scipy.stats import sem
from sklearn.feature_extraction.text import HashingVectorizer, TfidfVectorizer
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


TOKEN = re.compile(r"\w+")


def item_text(frame: pd.DataFrame) -> pd.Series:
    return frame["title"] + " " + frame["extract"]


def cross_terms(document: str) -> list[str]:
    """Query term x item term pairs, separately for the title and the extract.

    ``document`` is ``query, title, extract`` joined by the ASCII unit separator: HashingVectorizer hands its analyzer one
    string per row, so the three fields travel together.
    """
    query, title, extract = document.lower().split("\x1f")
    query_terms = set(TOKEN.findall(query))
    pairs = []
    for field, text in (("t", title), ("e", extract)):
        item_terms = set(TOKEN.findall(text))
        pairs += [f"{field}:{q}|{d}" for q in query_terms for d in item_terms]
    return pairs


def cross_documents(frame: pd.DataFrame) -> pd.Series:
    return frame["query"] + "\x1f" + frame["title"] + "\x1f" + frame["extract"]


class TextModel:
    """TF-IDF of the item text, optionally plus hashed query x item term crosses, into an LR."""

    def __init__(self, c: float, max_features: int, cross: bool, cross_features: int):
        self.vectorizer = TfidfVectorizer(
            ngram_range=(1, 2), min_df=2, max_features=max_features, sublinear_tf=True, strip_accents="unicode"
        )
        self.crosser = (
            HashingVectorizer(analyzer=cross_terms, n_features=cross_features, alternate_sign=False, norm="l2")
            if cross
            else None
        )
        self.classifier = LogisticRegression(C=c, max_iter=1000)

    def matrix(self, frame: pd.DataFrame, fit: bool):
        text = item_text(frame)
        blocks = [self.vectorizer.fit_transform(text) if fit else self.vectorizer.transform(text)]
        if self.crosser is not None:
            blocks.append(self.crosser.transform(cross_documents(frame)))
        return hstack(blocks).tocsr()

    def fit(self, frame: pd.DataFrame) -> "TextModel":
        self.classifier.fit(self.matrix(frame, fit=True), frame["label"])
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return self.classifier.predict_proba(self.matrix(frame, fit=False))[:, 1]


def out_of_fold_scores(make_model, train: pd.DataFrame, folds: int) -> np.ndarray:
    scores = np.zeros(len(train))
    for fit_index, score_index in GroupKFold(n_splits=folds).split(train, groups=train["qnorm"]):
        scores[score_index] = make_model().fit(train.iloc[fit_index]).predict(train.iloc[score_index])
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
    parser.add_argument("--cross-features", type=int, default=2**20, help="hash space for query x item crosses")
    args = parser.parse_args()

    _, llm = load_datasets()
    llm = llm.reset_index(drop=True)
    llm["label"] = (llm["overall"] >= args.overall_threshold).astype(float)
    base_features = rust_features(llm)
    print(f"{len(llm)} rows, {llm['qnorm'].nunique()} queries, positive rate {llm['label'].mean():.3f}")

    # name -> whether the text model includes query x item crosses
    text_models = {"text": False, "cross": True}
    arms = ["xgb"] + [f"xgb+{name}" for name in text_models] + list(text_models)
    results = {arm: [] for arm in arms}
    for fold, (train_index, test_index) in enumerate(GroupKFold(n_splits=args.folds).split(llm, groups=llm["qnorm"])):
        train, test = llm.iloc[train_index], llm.iloc[test_index]
        train_features, test_features = base_features[train_index], base_features[test_index]

        predictions = {"xgb": fit_xgb(train_features, train["label"]).predict_proba(test_features)[:, 1]}
        for name, cross in text_models.items():

            def make_model(cross=cross):
                return TextModel(args.c, args.max_features, cross, args.cross_features)

            train_scores = out_of_fold_scores(make_model, train, args.inner_folds)
            test_scores = make_model().fit(train).predict(test)
            predictions[name] = test_scores
            stacked = fit_xgb(np.column_stack([train_features, train_scores]), train["label"])
            predictions[f"xgb+{name}"] = stacked.predict_proba(np.column_stack([test_features, test_scores]))[:, 1]

        for arm in arms:
            results[arm].append(evaluate(PrecomputedPredictions(predictions[arm]), test))
        line = ", ".join(f"{arm} {results[arm][-1]['ndcg']:.4f}" for arm in arms)
        print(f"fold {fold + 1}/{args.folds}: ndcg {line}", flush=True)

    print("\n=== mean over folds ===")
    for arm in arms:
        per_fold = pd.DataFrame(results[arm])
        ndcg = per_fold["ndcg"]
        print(
            f"{arm:8s} ndcg {ndcg.mean():.4f} ± {sem(ndcg):.4f}  ndcg@10 {per_fold['ndcg@10'].mean():.4f}  "
            f"p@5 {per_fold['p@5'].mean():.4f}  p@10 {per_fold['p@10'].mean():.4f}"
        )
    baseline = pd.DataFrame(results["xgb"])["ndcg"]
    for name in text_models:
        gains = pd.DataFrame(results[f"xgb+{name}"])["ndcg"] - baseline
        print(f"xgb+{name} minus xgb, ndcg per fold: {np.round(gains.values, 4).tolist()} (mean {gains.mean():+.4f})")


if __name__ == "__main__":
    run()
