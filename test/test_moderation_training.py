"""
Training the domain-moderation model on synthetic, separable data: the metrics it reports,
the paired comparison against an incumbent, and the management command that drives it.
"""

import json

import numpy as np
import pytest
from django.core.management import call_command

from mwmbl.management.commands import train_domain_moderation_model as command
from mwmbl.moderation.train import (
    compare_models,
    evaluate,
    normalised_ap_of,
    operating_point,
    recall_at_precision,
    split_by_time,
    train,
)
from mwmbl.moderation.training_data import DERIVED, REAL, SEED, TrainingRow

REJECT_TEXT = "cheap pills casino bonus click here win money fast"
APPROVE_TEXT = "community garden recipes local history library volunteers"


def _rows(count: int = 160) -> list[TrainingRow]:
    """Alternating approve/reject decisions, oldest first, from a few known submitters then new ones."""
    rows = []
    for i in range(count):
        rejected = i % 2 == 0
        late = i >= count * 0.7
        rows.append(
            TrainingRow(
                domain=f"{'spam' if rejected else 'site'}{i}.example",
                rejected=rejected,
                reason=("SPAM" if i % 4 == 0 else "OFFENSIVE") if rejected else "",
                source=REAL,
                page_texts=[REJECT_TEXT if rejected else APPROVE_TEXT],
                timestamp=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
                submitter=f"cold{i}" if late else f"known{i % 3}",
                suggested_status="REJECTED" if i % 5 == 0 else "",
            )
        )
    return rows


@pytest.fixture(scope="module")
def trained():
    return train(_rows(), "test-v1")


def test_split_keeps_the_newest_real_rows_for_testing_and_all_other_sources_for_training():
    other = [
        TrainingRow(domain="seed.example", rejected=True, reason="SPAM", source=SEED, page_texts=[]),
        TrainingRow(domain="derived.example", rejected=True, reason="SPAM", source=DERIVED, page_texts=[]),
    ]

    train_rows, test_rows = split_by_time(_rows(40) + other)

    assert len(test_rows) == 10
    assert {row.source for row in test_rows} == {REAL}
    assert min(row.timestamp for row in test_rows) > max(row.timestamp for row in train_rows if row.source == REAL)
    assert {row.domain for row in other} <= {row.domain for row in train_rows}


def test_a_trained_model_separates_the_classes(trained):
    model, metrics = trained

    assert metrics["all"]["normalised_ap"] > 0.9
    assert metrics["cold_start"]["rows"] > 0
    assert metrics["uses_text"] is True
    assert metrics["uses_has_text"] is False
    assert metrics["train_rows_by_source"] == {REAL: 120}
    assert "reason_head" in metrics
    assert model.version == "test-v1"
    assert "spam0.example" in model.train_domains


def test_metrics_are_json_serialisable(trained):
    _, metrics = trained

    assert json.loads(json.dumps(metrics))["all"]["rows"] == metrics["all"]["rows"]


def test_without_text_the_featuriser_has_no_text_block():
    _, metrics = train(_rows(), "no-text", use_text=False)

    assert metrics["uses_text"] is False


def test_evaluating_with_no_held_out_rows_says_so(trained):
    model, _ = trained

    assert evaluate(model, [], []) == {"error": "no held-out real rows to evaluate on"}


def test_candidate_is_compared_with_the_incumbent_on_the_same_rows(trained):
    incumbent, _ = trained

    _, metrics = train(_rows(), "test-v2", incumbent=incumbent)

    comparison = metrics["versus_incumbent"]
    assert comparison["incumbent_version"] == "test-v1"
    assert comparison["rows"] > 0
    assert comparison["difference_ci"][0] <= comparison["difference"] <= comparison["difference_ci"][1]


def test_compare_models_runs_on_a_test_split(trained):
    model, _ = trained
    _, test_rows = split_by_time(_rows())
    cold = np.array([True] * len(test_rows))
    scores = np.array([prediction[0] for prediction in model.predict([row.to_example() for row in test_rows])])

    comparison = compare_models(model, model, test_rows, cold, scores)

    assert comparison["difference"] == pytest.approx(0.0)


def test_operating_point_reports_none_where_a_threshold_selects_nothing():
    truth = np.array([1, 0, 1, 0])
    scores = np.array([0.5, 0.5, 0.5, 0.5])

    point = operating_point(truth, scores)

    assert point["reject_precision"] is None
    assert point["approve_error_rate"] is None
    assert point["unsure_share"] == 1.0


def test_recall_at_precision_for_a_perfect_ranking():
    truth = np.array([1, 1, 0, 0])
    scores = np.array([0.9, 0.8, 0.2, 0.1])

    assert recall_at_precision(truth, scores) == 1.0


def test_normalised_ap_reads_both_metric_formats():
    current = {"normalised_ap": 0.8, "normalised_ap_ci": [0.6, 0.9]}
    legacy = {"rows": 100, "positives": 20, "pr_auc": 0.6, "pr_auc_ci": [0.5, 0.7]}

    assert normalised_ap_of(current) == (0.8, 0.6)
    point, floor = normalised_ap_of(legacy)
    assert point == pytest.approx((0.6 - 0.2) / 0.8)
    assert floor == pytest.approx((0.5 - 0.2) / 0.8)
    assert normalised_ap_of({}) is None


@pytest.fixture
def patched_command(monkeypatch):
    rows = _rows()
    published = []
    monkeypatch.setattr(command.training_data, "real_rows", lambda: rows)
    monkeypatch.setattr(command.training_data, "seed_rows", lambda: [])
    monkeypatch.setattr(command, "load_published_model", lambda: None)
    monkeypatch.setattr(command, "load_metrics", lambda: {})
    monkeypatch.setattr(command, "publish", lambda model, metrics: published.append(model.version))
    monkeypatch.setattr(command, "rescore_pending_submissions", lambda schedule: published.append("rescore"))
    return published


def test_dry_run_trains_and_reports_without_publishing(patched_command, capsys):
    call_command("train_domain_moderation_model", "--dry-run")

    output = capsys.readouterr().out
    assert "PASS" in output
    assert "Dry run" in output
    assert patched_command == []


def test_a_passing_run_publishes_and_rescores_the_queue(patched_command, capsys):
    call_command("train_domain_moderation_model")

    assert patched_command[0].startswith("domain-mod-")
    assert patched_command[1] == "rescore"


def test_a_failing_gate_blocks_publishing_unless_forced(patched_command, monkeypatch, capsys):
    monkeypatch.setattr(command, "passes_gate", lambda new, current: (False, "worse"))

    call_command("train_domain_moderation_model")
    assert patched_command == []

    call_command("train_domain_moderation_model", "--force")
    assert patched_command[1] == "rescore"


def test_ablation_reports_each_variant_and_the_text_coverage(patched_command, capsys):
    call_command("train_domain_moderation_model", "--dry-run", "--ablate")

    output = capsys.readouterr().out
    assert "domain name only" in output
    assert "text + has_text" in output
    assert "cold-start test rows" in output


def test_no_decided_submissions_means_nothing_to_train(monkeypatch, capsys):
    monkeypatch.setattr(command.training_data, "real_rows", lambda: [])

    call_command("train_domain_moderation_model")

    assert "No decided submissions" in capsys.readouterr().err


@pytest.mark.django_db
def test_derived_rows_are_requested_for_reasons_short_of_real_data(patched_command, monkeypatch, capsys):
    requested = []

    def fake_derived(reasons, per_reason, exclude):
        requested.append((set(reasons), per_reason))
        return []

    monkeypatch.setattr(command.training_data, "derived_rows", fake_derived)

    call_command("train_domain_moderation_model", "--dry-run", "--derived")

    assert requested
    reasons, per_reason = requested[0]
    assert per_reason == command.DERIVED_ROWS_PER_REASON
    assert reasons
