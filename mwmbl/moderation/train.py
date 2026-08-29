"""Fit and evaluate the moderation suggester.

The evaluation is the point of this module, not the fitting. Two things about this data make
a naive accuracy number actively misleading:

* One prolific submitter accounts for 799 of the last 949 decisions at a 1% rejection rate,
  so an aggregate score is dominated by rows no model is needed for. Cold-start submitters -
  the 16% where a moderator is genuinely deciding something - reject at 54%, and that slice is
  what the gate reads.
* That slice has ~81 positives, where point estimates move around far more than they look
  like they do. Bootstrapping the augmentation experiment turned a 0.04 PR-AUC "improvement"
  into two intervals spanning 0.17 that overlapped almost entirely. So the gate compares
  intervals, and every metric is reported with one.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from logging import getLogger
from typing import Optional

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, precision_recall_curve

from mwmbl.moderation.features import Featuriser
from mwmbl.moderation.model import ModerationModel
from mwmbl.moderation.training_data import REAL, TrainingRow

logger = getLogger(__name__)

TEST_FRACTION = 0.25
BOOTSTRAP_SAMPLES = 400
MIN_CLASS_ROWS = 5


@dataclass
class Evaluation:
    slice_name: str
    rows: int
    positives: int
    pr_auc: float
    pr_auc_ci: tuple[float, float]
    recall_at_precision_75: float

    def to_dict(self) -> dict:
        return {
            "slice": self.slice_name,
            "rows": self.rows,
            "positives": self.positives,
            "pr_auc": round(self.pr_auc, 4),
            "pr_auc_ci": [round(self.pr_auc_ci[0], 4), round(self.pr_auc_ci[1], 4)],
            "recall_at_precision_75": round(self.recall_at_precision_75, 4),
        }


def split_by_time(rows: list[TrainingRow]) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Time-ordered split of the real rows; derived and seed rows always go to training.

    Chronological rather than random because moderation drifts - the .ai spam wave is a 2026
    phenomenon that does not appear in 2024 - and a random split would let the model learn
    from the future it is being tested on.
    """
    real = sorted((row for row in rows if row.source == REAL),
                  key=lambda row: row.timestamp or "")
    other = [row for row in rows if row.source != REAL]
    cut = int((1 - TEST_FRACTION) * len(real))
    return real[:cut] + other, real[cut:]


def train(rows: list[TrainingRow], version: str) -> tuple[ModerationModel, dict]:
    """Fit both heads and evaluate on held-out real rows only."""
    train_rows, test_rows = split_by_time(rows)
    logger.info("Training on %d rows (%d real), testing on %d real rows",
                len(train_rows), sum(r.source == REAL for r in train_rows), len(test_rows))

    featuriser = Featuriser()
    features = featuriser.fit_transform([row.to_example() for row in train_rows])

    # The binary head sees real rows only: blanket augmentation was measured and did not help.
    real_mask = np.array([row.source == REAL for row in train_rows])
    reject_head = LogisticRegression(max_iter=4000, C=4.0, class_weight="balanced")
    reject_head.fit(features[real_mask],
                    [row.rejected for row, keep in zip(train_rows, real_mask) if keep])

    # The reason head sees every source, because that is the whole point of the derived rows.
    reason_mask = np.array([row.rejected and bool(row.reason) for row in train_rows])
    reason_head = LogisticRegression(max_iter=4000, C=4.0, class_weight="balanced")
    reason_head.fit(features[reason_mask],
                    [row.reason for row, keep in zip(train_rows, reason_mask) if keep])

    model = ModerationModel(featuriser, reject_head, reason_head, version)
    return model, evaluate(model, train_rows, test_rows)


def evaluate(model: ModerationModel, train_rows: list[TrainingRow],
             test_rows: list[TrainingRow]) -> dict:
    if not test_rows:
        return {"error": "no held-out real rows to evaluate on"}

    predictions = model.predict([row.to_example() for row in test_rows])
    reject_scores = np.array([prediction[0] for prediction in predictions])
    truth = np.array([row.rejected for row in test_rows], dtype=int)

    # The slice that matters. One prolific submitter accounts for most decisions at a 1%
    # rejection rate, so an aggregate score mostly measures rows no model is needed for.
    # Submissions from an account with no track record in the training window reject at 54%,
    # and those are the ones a moderator is genuinely deciding.
    known_submitters = {row.submitter for row in train_rows if row.submitter}
    cold = np.array([row.submitter not in known_submitters for row in test_rows])

    slices = {"all": np.ones(len(test_rows), dtype=bool), "cold_start": cold}
    metrics = {
        name: _evaluate_slice(name, truth[mask], reject_scores[mask]).to_dict()
        for name, mask in slices.items()
        if _has_both_classes(truth[mask])
    }
    metrics["reason_head"] = _evaluate_reason_head(predictions, test_rows)
    metrics["train_rows_by_source"] = _count_sources(train_rows)
    metrics["suggestion_influence"] = _suggestion_influence(train_rows + test_rows)
    return metrics


def _evaluate_slice(name: str, truth: np.ndarray, scores: np.ndarray) -> Evaluation:
    generator = np.random.default_rng(0)
    samples = []
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = generator.choice(len(truth), len(truth), replace=True)
        if _has_both_classes(truth[indices]):
            samples.append(average_precision_score(truth[indices], scores[indices]))

    precision, recall, _ = precision_recall_curve(truth, scores)
    return Evaluation(
        slice_name=name,
        rows=len(truth),
        positives=int(truth.sum()),
        pr_auc=float(average_precision_score(truth, scores)),
        pr_auc_ci=(float(np.percentile(samples, 2.5)) if samples else 0.0,
                   float(np.percentile(samples, 97.5)) if samples else 0.0),
        recall_at_precision_75=max(
            (r for p, r in zip(precision, recall) if p >= 0.75 and r > 0), default=0.0),
    )


def _evaluate_reason_head(predictions, test_rows: list[TrainingRow]) -> dict:
    """Per-reason F1 on the real rejections we held out.

    Per class rather than averaged, because the classes are not comparable: SPAM has 208 real
    examples behind it and OFFENSIVE has one. An average would let a collapse in the class
    that works be hidden by the class that never did.
    """
    truth, predicted = [], []
    for prediction, row in zip(predictions, test_rows):
        if row.rejected and row.reason:
            truth.append(row.reason)
            predicted.append(prediction[1])
    if not truth:
        return {"note": "no held-out real rejections with a reason"}

    labels = sorted(set(truth))
    scores = f1_score(truth, predicted, labels=labels, average=None, zero_division=0)
    counts = {label: truth.count(label) for label in labels}
    return {
        "rows": len(truth),
        "f1": {label: round(float(score), 4) for label, score in zip(labels, scores)},
        # Reported alongside the scores because the rare reasons are *very* rare in a
        # chronological split - LANGUAGE lands 2 rows in a typical test set - and an F1 of 0.0
        # on 2 rows is noise that reads like a broken class.
        "support": counts,
    }


def _suggestion_influence(rows: list[TrainingRow]) -> dict:
    """How much of the training data was decided with a suggestion on screen, and how often
    the moderator agreed with it.

    A retrain learns from decisions the previous model influenced, and the gate cannot see
    that: the held-out labels come from the same influenced population, so a model that has
    taught moderators its own mistakes scores well on them. Nothing here corrects for it -
    reweighting on an unmeasured hunch is exactly what this module refuses to do elsewhere -
    but a rising ``agreed`` against a rising ``shown`` is the shape to watch for, and it is
    the argument for down-weighting confirmations when there is enough data to test it on.
    """
    real = [row for row in rows if row.source == REAL]
    shown = [row for row in real if row.suggested_status]
    agreed = [row for row in shown
              if (row.suggested_status == "REJECTED") == row.rejected]
    return {
        "real_rows": len(real),
        "shown_a_suggestion": len(shown),
        "agreed_with_it": len(agreed),
    }


def _has_both_classes(truth: np.ndarray) -> bool:
    return truth.sum() >= MIN_CLASS_ROWS and (1 - truth).sum() >= MIN_CLASS_ROWS


def _count_sources(rows: list[TrainingRow]) -> dict:
    return dict(Counter(row.source for row in rows))


def passes_gate(new: dict, current: dict) -> tuple[bool, str]:
    """Whether a freshly trained model may replace the deployed one.

    The comparison is on the cold-start slice, and against the *lower* bound of the incumbent's
    interval rather than its point estimate. A gate on point estimates would have shipped the
    augmentation change that bootstrapping later showed to be noise.
    """
    new_cold = new.get("cold_start")
    if new_cold is None:
        return False, "no cold-start slice in the new metrics - not enough held-out data"

    current_cold = current.get("cold_start")
    if current_cold is None:
        return True, f"no incumbent to compare against; cold-start PR-AUC {new_cold['pr_auc']:.3f}"

    floor = current_cold["pr_auc_ci"][0]
    if new_cold["pr_auc"] < floor:
        return False, (f"cold-start PR-AUC {new_cold['pr_auc']:.3f} is below the incumbent's "
                       f"lower bound {floor:.3f}")
    return True, (f"cold-start PR-AUC {new_cold['pr_auc']:.3f} "
                  f"(incumbent {current_cold['pr_auc']:.3f}, lower bound {floor:.3f})")
