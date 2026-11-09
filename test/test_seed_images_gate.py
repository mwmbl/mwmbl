"""The Seed Search images gate and ranking metrics (no network)."""

import numpy as np
import pytest

from mwmbl.rankeval.seed_images import gate_analysis


def test_ndcg():
    assert gate_analysis.ndcg([3, 0, 1], 3) == pytest.approx((7 + 1 / 2) / (7 + 1 / np.log2(3)))
    assert gate_analysis.ndcg([3, 1, 0], 2) == 1.0
    assert gate_analysis.ndcg([0, 0], 1) is None


def test_best_threshold_maximises_f1():
    scores = np.array([0.5, 1.0, 2.0, 2.5])
    positives = np.array([False, False, True, True])

    assert gate_analysis.best_threshold(scores, positives) == 2.0


def test_mix_order_moves_from_jev_to_commons_order_as_the_weight_grows():
    images = [{"commons_rank": 1, "jev": 1.0}, {"commons_rank": 2, "jev": 2.0}]

    assert [image["commons_rank"] for image in gate_analysis.mix_order(images, 0.0)] == [2, 1]
    assert [image["commons_rank"] for image in gate_analysis.mix_order(images, 100.0)] == [1, 2]


def test_shown_counts_images_above_the_minimum_relevance():
    pool = [
        {"commons_rank": 1, "jev": 2.6, "relevance": 3},
        {"commons_rank": 2, "jev": 2.2, "relevance": 0},
        {"commons_rank": 3, "jev": 0.5, "relevance": 2},
    ]

    stats = gate_analysis.shown([pool], 3, 2.0)

    assert (stats["per_query"], stats["good"], stats["wrong"]) == (2, 0.5, 0.5)


def test_agreement_rounds_scores_for_exact_matches():
    stats = gate_analysis.agreement([0.2, 1.6, 2.9], [0, 1, 3])

    assert stats["exact"] == pytest.approx(2 / 3)
    assert stats["within_one"] == 1.0
    assert stats["spearman"] == pytest.approx(1.0)


def test_bootstrap_interval_brackets_the_mean_and_collapses_for_a_constant():
    values = np.arange(100, dtype=float)

    low, high = gate_analysis.bootstrap_interval(lambda sample: values[sample].mean(), len(values))

    assert low < values.mean() < high
    assert gate_analysis.bootstrap_interval(lambda sample: 1.0, 10) == (1.0, 1.0)
