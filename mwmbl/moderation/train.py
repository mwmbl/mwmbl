"""Fit and evaluate the moderation suggester.

The evaluation is the point of this module, not the fitting. Three things about this data make
a naive accuracy number actively misleading:

* One prolific submitter accounts for most decisions at a ~1% rejection rate, so an aggregate
  score is dominated by rows no model is needed for. Cold-start submitters - the minority where
  a moderator is genuinely deciding something - reject far more often, and that slice is what
  the gate reads.
* That slice has a couple of hundred positives, where point estimates move around far more than
  they look like they do. Bootstrapping the augmentation experiment turned a 0.04 PR-AUC
  "improvement" into two intervals spanning 0.17 that overlapped almost entirely. So every
  metric is reported with an interval.
* **PR-AUC's floor is the positive rate, so it is not comparable across populations.** The
  August 2026 retrain looked like a 0.11 regression (0.779 -> 0.672 cold-start PR-AUC) purely
  because migration 0037 repaired 1,785 previously-invisible decisions into the training set
  and moved the cold-start slice's rejection rate from 54% to 30%. On the prevalence-corrected
  measure the "regression" was a small *improvement* (0.519 -> 0.532). Every slice therefore
  reports ``base_rate`` and ``normalised_ap`` - ``(AP - base) / (1 - base)``, which is 0 for a
  coin flip and 1 for a perfect ranker regardless of prevalence - and the gate reads that.

The gate's real answer, though, is not a number compared against a stored number from a run on
a different population. It is :func:`compare_models`: score the incumbent and the candidate on
the *same* held-out rows and bootstrap the *difference*, which cancels the shared test-set
variance and is far more sensitive than comparing two independent intervals.
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

# Non-inferiority margin, in normalised-AP points, for the paired comparison against the
# incumbent. Requiring the difference to be significantly *positive* would ship nothing: with a
# couple of hundred positives, a genuine improvement of a few points does not clear zero. So the
# gate asks the answerable question instead - are we confident this is not meaningfully worse -
# and 0.05 is about a third of the width of a typical interval on this data.
GATE_TOLERANCE = 0.05


@dataclass
class Evaluation:
    slice_name: str
    rows: int
    positives: int
    base_rate: float
    pr_auc: float
    pr_auc_ci: tuple[float, float]
    normalised_ap: float
    normalised_ap_ci: tuple[float, float]
    recall_at_precision_75: float
    rows_with_text: int

    def to_dict(self) -> dict:
        return {
            "slice": self.slice_name,
            "rows": self.rows,
            "positives": self.positives,
            # Reported next to every score because PR-AUC cannot be read without it: the same
            # model scores 0.78 on a 54%-positive slice and 0.67 on a 30%-positive one.
            "base_rate": round(self.base_rate, 4),
            "pr_auc": round(self.pr_auc, 4),
            "pr_auc_ci": [round(self.pr_auc_ci[0], 4), round(self.pr_auc_ci[1], 4)],
            "normalised_ap": round(self.normalised_ap, 4),
            "normalised_ap_ci": [round(self.normalised_ap_ci[0], 4),
                                 round(self.normalised_ap_ci[1], 4)],
            "recall_at_precision_75": round(self.recall_at_precision_75, 4),
            # Evidence is crawled newest-first, so this differs sharply between the splits and
            # is the number to look at before believing anything about the text features.
            "rows_with_text": self.rows_with_text,
        }


def normalise(average_precision: float, base_rate: float) -> float:
    """PR-AUC rescaled so chance is 0 and perfect is 1, making it comparable across slices."""
    return (average_precision - base_rate) / (1 - base_rate)


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


def train(rows: list[TrainingRow], version: str, use_text: bool = True,
          incumbent: Optional[ModerationModel] = None) -> tuple[ModerationModel, dict]:
    """Fit both heads and evaluate on held-out real rows only.

    ``use_text=False`` fits the domain-name-only model, which is the ablation that answers
    whether the crawled page text is earning its place. ``incumbent`` is the model currently
    being served; when given, the metrics carry a paired comparison against it on these exact
    test rows, which is the only comparison that means anything.
    """
    train_rows, test_rows = split_by_time(rows)
    logger.info("Training on %d rows (%d real), testing on %d real rows",
                len(train_rows), sum(r.source == REAL for r in train_rows), len(test_rows))

    featuriser = Featuriser(use_text=use_text)
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

    model = ModerationModel(featuriser, reject_head, reason_head, version,
                            train_domains={row.domain for row in train_rows})
    return model, evaluate(model, train_rows, test_rows, incumbent)


def evaluate(model: ModerationModel, train_rows: list[TrainingRow],
             test_rows: list[TrainingRow],
             incumbent: Optional[ModerationModel] = None) -> dict:
    if not test_rows:
        return {"error": "no held-out real rows to evaluate on"}

    examples = [row.to_example() for row in test_rows]
    predictions = model.predict(examples)
    reject_scores = np.array([prediction[0] for prediction in predictions])
    truth = np.array([row.rejected for row in test_rows], dtype=int)
    has_text = np.array([bool(row.to_example().text.strip()) for row in test_rows])

    # The slice that matters. One prolific submitter accounts for most decisions at a 1%
    # rejection rate, so an aggregate score mostly measures rows no model is needed for.
    # Submissions from an account with no track record in the training window reject far more
    # often, and those are the ones a moderator is genuinely deciding.
    known_submitters = {row.submitter for row in train_rows if row.submitter}
    cold = np.array([row.submitter not in known_submitters for row in test_rows])

    slices = {"all": np.ones(len(test_rows), dtype=bool), "cold_start": cold}
    metrics = {
        name: _evaluate_slice(name, truth[mask], reject_scores[mask], has_text[mask]).to_dict()
        for name, mask in slices.items()
        if _has_both_classes(truth[mask])
    }
    metrics["reason_head"] = _evaluate_reason_head(predictions, test_rows)
    metrics["train_rows_by_source"] = _count_sources(train_rows)
    # Fit-time text coverage, alongside the per-slice test coverage above. A large gap between
    # the two is the train/test skew that makes the text block look worse than it is.
    metrics["train_rows_with_text"] = sum(
        1 for row in train_rows if row.to_example().text.strip())
    metrics["uses_text"] = model.featuriser.text is not None
    metrics["suggestion_influence"] = _suggestion_influence(train_rows + test_rows)
    if incumbent is not None:
        metrics["versus_incumbent"] = compare_models(
            model, incumbent, test_rows, cold, reject_scores)
    return metrics


def compare_models(candidate: ModerationModel, incumbent: ModerationModel,
                   test_rows: list[TrainingRow], cold: np.ndarray,
                   candidate_scores: np.ndarray) -> dict:
    """Paired bootstrap of candidate minus incumbent on identical cold-start rows.

    Two models scored on the same rows share all the variance that comes from *which* rows
    landed in the test set, and resampling them together cancels it. Comparing two independent
    intervals - what the gate used to do, across two different test sets - throws that away and
    then blames the model for the difference.

    Rows whose domain the incumbent was trained on are dropped. Scoring a model on its own
    training data flatters it, and since the incumbent is the thing being defended, leaving them
    in biases the comparison *against* the candidate. Older artifacts carry no record of what
    they trained on; the comparison still runs, and says so, because a candidate that wins under
    a bias in the incumbent's favour has won.
    """
    incumbent_scores = np.array([prediction[0] for prediction in
                                 incumbent.predict([row.to_example() for row in test_rows])])

    contamination_known = bool(incumbent.train_domains)
    seen = incumbent.train_domains or set()
    clean = np.array([row.domain not in seen for row in test_rows])
    mask = cold & clean
    if not _has_both_classes(np.array([row.rejected for row in test_rows], dtype=int)[mask]):
        return {"note": "not enough uncontaminated cold-start rows to compare on",
                "incumbent_version": incumbent.version}

    truth = np.array([row.rejected for row in test_rows], dtype=int)[mask]
    candidate_slice = candidate_scores[mask]
    incumbent_slice = incumbent_scores[mask]

    generator = np.random.default_rng(0)
    differences = []
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = generator.choice(len(truth), len(truth), replace=True)
        if not _has_both_classes(truth[indices]):
            continue
        base = truth[indices].mean()
        differences.append(
            normalise(average_precision_score(truth[indices], candidate_slice[indices]), base)
            - normalise(average_precision_score(truth[indices], incumbent_slice[indices]), base))

    base_rate = float(truth.mean())
    difference = (normalise(average_precision_score(truth, candidate_slice), base_rate)
                  - normalise(average_precision_score(truth, incumbent_slice), base_rate))
    return {
        "slice": "cold_start",
        "incumbent_version": incumbent.version,
        "rows": int(mask.sum()),
        "positives": int(truth.sum()),
        "rows_dropped_as_incumbent_training_data": int((cold & ~clean).sum()),
        # False means the incumbent predates train-domain recording, so some of these rows may
        # be its own training data and the comparison leans in its favour.
        "contamination_known": contamination_known,
        "candidate_normalised_ap": round(
            normalise(average_precision_score(truth, candidate_slice), base_rate), 4),
        "incumbent_normalised_ap": round(
            normalise(average_precision_score(truth, incumbent_slice), base_rate), 4),
        "difference": round(difference, 4),
        "difference_ci": [round(float(np.percentile(differences, 2.5)), 4),
                          round(float(np.percentile(differences, 97.5)), 4)],
        # How often the candidate won a resample. Reads more directly than the interval when
        # the interval straddles zero, which on this much data it usually will.
        "candidate_wins_fraction": round(float(np.mean(np.array(differences) > 0)), 4),
    }


def _evaluate_slice(name: str, truth: np.ndarray, scores: np.ndarray,
                    has_text: np.ndarray) -> Evaluation:
    generator = np.random.default_rng(0)
    samples, normalised = [], []
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = generator.choice(len(truth), len(truth), replace=True)
        if not _has_both_classes(truth[indices]):
            continue
        average_precision = average_precision_score(truth[indices], scores[indices])
        samples.append(average_precision)
        # Normalised per resample, against that resample's own base rate: the resampling itself
        # moves prevalence around, and correcting with the whole-slice rate would leave that in.
        normalised.append(normalise(average_precision, truth[indices].mean()))

    precision, recall, _ = precision_recall_curve(truth, scores)
    base_rate = float(truth.mean())
    return Evaluation(
        slice_name=name,
        rows=len(truth),
        positives=int(truth.sum()),
        base_rate=base_rate,
        pr_auc=float(average_precision_score(truth, scores)),
        pr_auc_ci=(float(np.percentile(samples, 2.5)) if samples else 0.0,
                   float(np.percentile(samples, 97.5)) if samples else 0.0),
        normalised_ap=normalise(float(average_precision_score(truth, scores)), base_rate),
        normalised_ap_ci=(float(np.percentile(normalised, 2.5)) if normalised else 0.0,
                          float(np.percentile(normalised, 97.5)) if normalised else 0.0),
        recall_at_precision_75=max(
            (r for p, r in zip(precision, recall) if p >= 0.75 and r > 0), default=0.0),
        rows_with_text=int(has_text.sum()),
    )


def _evaluate_reason_head(predictions, test_rows: list[TrainingRow]) -> dict:
    """Per-reason F1 on the real rejections we held out.

    Per class rather than averaged, because the classes are not comparable: SPAM has hundreds of
    real examples behind it and OFFENSIVE has one. An average would let a collapse in the class
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
    # suggested_status holds the *action* the tool displayed - APPROVE, REJECT or UNSURE -
    # and not a submission status, so it is never "REJECTED". A suggestion of UNSURE takes no
    # side, and a decision made against one agrees with nothing: it counts as shown, but is
    # not part of the population agreement is measured over.
    took_a_side = [row for row in shown if row.suggested_status in ("APPROVE", "REJECT")]
    agreed = [row for row in took_a_side
              if (row.suggested_status == "REJECT") == row.rejected]
    return {
        "real_rows": len(real),
        "shown_a_suggestion": len(shown),
        "suggested_a_side": len(took_a_side),
        "agreed_with_it": len(agreed),
    }


def _has_both_classes(truth: np.ndarray) -> bool:
    return truth.sum() >= MIN_CLASS_ROWS and (1 - truth).sum() >= MIN_CLASS_ROWS


def _count_sources(rows: list[TrainingRow]) -> dict:
    return dict(Counter(row.source for row in rows))


def normalised_ap_of(slice_metrics: dict) -> Optional[tuple[float, float]]:
    """Point estimate and interval lower bound in normalised AP, from either metrics format.

    Artifacts stored before normalised AP existed recorded ``pr_auc`` and enough to reconstruct
    the base rate, so a retrain landing on top of one is not forced back to comparing raw PR-AUC
    across populations - the mistake this whole module exists to stop. Read through this on both
    sides of the comparison rather than only the incumbent's, so a rollback to older code, or a
    hand-written metrics blob, is a weaker comparison rather than a KeyError in the gate.
    """
    if "normalised_ap" in slice_metrics:
        return slice_metrics["normalised_ap"], slice_metrics["normalised_ap_ci"][0]
    if not slice_metrics.get("rows"):
        return None
    base_rate = slice_metrics["positives"] / slice_metrics["rows"]
    return (normalise(slice_metrics["pr_auc"], base_rate),
            normalise(slice_metrics["pr_auc_ci"][0], base_rate))


def passes_gate(new: dict, current: dict) -> tuple[bool, str]:
    """Whether a freshly trained model may replace the deployed one.

    Preference order, best evidence first:

    1. The paired comparison on identical rows, when the incumbent model could be loaded. The
       candidate ships unless it is *confidently* worse by more than :data:`GATE_TOLERANCE`.
    2. Otherwise, normalised AP against the lower bound of the incumbent's stored interval.
       Weaker, because the two numbers come from different test sets - but at least it is not
       comparing raw PR-AUC across different prevalences, which failed the August 2026 retrain
       for a regression that had not happened.
    """
    new_cold = new.get("cold_start")
    if new_cold is None:
        return False, "no cold-start slice in the new metrics - not enough held-out data"

    candidate = normalised_ap_of(new_cold)
    if candidate is None:
        return False, "the new cold-start metrics cannot be read"

    comparison = new.get("versus_incumbent")
    if comparison is not None and "difference" in comparison:
        caveat = "" if comparison["contamination_known"] else (
            "; the incumbent has no record of its training domains, so some of these rows may "
            "be its own and the comparison favours it")
        if comparison["difference_ci"][0] < -GATE_TOLERANCE:
            return False, (
                f"paired against {comparison['incumbent_version']} on {comparison['rows']} "
                f"cold-start rows, normalised AP is {comparison['difference']:+.3f} with "
                f"interval [{comparison['difference_ci'][0]:.3f}, "
                f"{comparison['difference_ci'][1]:.3f}], which admits a loss worse than the "
                f"{GATE_TOLERANCE:.2f} tolerance{caveat}")
        return True, (
            f"paired against {comparison['incumbent_version']} on {comparison['rows']} "
            f"cold-start rows, normalised AP is {comparison['difference']:+.3f} "
            f"[{comparison['difference_ci'][0]:.3f}, {comparison['difference_ci'][1]:.3f}], "
            f"winning {comparison['candidate_wins_fraction']:.0%} of resamples{caveat}")

    current_cold = current.get("cold_start")
    incumbent = normalised_ap_of(current_cold) if current_cold else None
    if incumbent is None:
        return True, (f"no readable incumbent to compare against; cold-start normalised AP "
                      f"{candidate[0]:.3f}")

    point, floor = incumbent
    if candidate[0] < floor:
        return False, (
            f"cold-start normalised AP {candidate[0]:.3f} is below the incumbent's lower bound "
            f"{floor:.3f} (unpaired: the incumbent model could not be loaded, so these are "
            f"different test sets and the comparison is weak)")
    return True, (f"cold-start normalised AP {candidate[0]:.3f} "
                  f"(incumbent {point:.3f}, lower bound {floor:.3f}; unpaired)")
