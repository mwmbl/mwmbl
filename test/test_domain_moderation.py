"""Tests for the domain moderation suggester and the moderator queue API.

The load-bearing property here is not that the model is accurate - that is measured offline by
the training gate - but that a moderator is never shown something misleading: a suggestion is
either backed by evidence or absent, a deterministic check always beats a probability, and the
queue never runs a model on the request path.
"""
import io
import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

import joblib
import numpy as np
import pytest
from allauth.account.models import EmailAddress
from background_task.models import Task
from django.contrib.auth.models import Permission
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import (
    DomainEvidence, DomainSubmission, ModerationModelArtifact, MwmblUser, SearchResultVote)
from mwmbl.moderation import model as model_module
from mwmbl.moderation import rules
from mwmbl.moderation.evidence import crawl_domain
from mwmbl.moderation.features import Featuriser, ModerationExample
from mwmbl.moderation.model import (
    Suggestion, get_model, load_metrics, publish, reset_model_cache, suggest)
from mwmbl.moderation.suggest import annotate_queue, refresh_suggestion, suggestion_for
from mwmbl.moderation.train import _suggestion_influence, operating_point, passes_gate
from mwmbl.moderation.training_data import TrainingRow, is_trainable_domain

QUEUE_URL = "/api/v1/platform/domain-submissions/queue"


@pytest.fixture
def submitter(db):
    return MwmblUser.objects.create_user(
        username="submitter", email="submitter@example.com", password="password")


@pytest.fixture(autouse=True)
def clear_the_model_cache():
    """The loaded model is a process-wide singleton, and several tests here publish one."""
    reset_model_cache()
    yield
    reset_model_cache()


@pytest.fixture
def established(db):
    """A submitter with a track record, whose approvals are not withheld."""
    user = MwmblUser.objects.create_user(
        username="established", email="established@example.com", password="password")
    for index in range(3):
        DomainSubmission.objects.create(name=f"past{index}.example", submitted_by=user,
                                        status="APPROVED")
    return user


@pytest.fixture
def moderator(db):
    user = MwmblUser.objects.create_user(
        username="moderator", email="moderator@example.com", password="password")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    user.user_permissions.add(
        Permission.objects.get(codename="change_domain_submission_status"))
    return user


def token(user):
    return f"Bearer {RefreshToken.for_user(user).access_token}"


def ready_evidence(domain, **overrides):
    defaults = {
        "state": DomainEvidence.State.READY,
        "fetched_at": timezone.now(),
        "http_status": 200,
        "pages": [{"url": f"https://{domain}/", "status": 200, "title": "A title",
                   "extract": "Some body text", "num_links": 12, "error": ""}],
        "signals": {"has_links": True, "num_pages_fetched": 1, "blacklisted": False},
        "suggested_action": "APPROVE",
        "confidence": 0.8,
        "reason_source": "model",
        "evidence": [],
        "model_version": "test-model",
    }
    fields = defaults | overrides
    # A stored REJECT always names a reason - the reason head returns one of its classes, and
    # every check that implies REJECT implies OTHER - and suggestion_for will not show one
    # that does not. A fixture without a reason would be a row that cannot occur.
    if fields["suggested_action"] == "REJECT" and not fields.get("suggested_reason"):
        fields["suggested_reason"] = "SPAM"
    return DomainEvidence.objects.create(domain=domain, **fields)


# --------------------------------------------------------------------- rules

@pytest.mark.parametrize("crawl,expected_kind", [
    ({"http_status": 404, "pages": [{"title": "", "extract": "", "num_links": 0}],
      "signals": {}}, "http_status"),
    ({"http_status": None, "error": "RobotsDenied", "pages": [], "signals": {}}, "robots"),
    ({"http_status": None, "error": "AbortError", "pages": [], "signals": {}}, "unreachable"),
    ({"http_status": 200, "final_domain": "somewhere-else.com", "pages": [],
      "signals": {}}, "redirect"),
    ({"http_status": 200, "pages": [{"title": "t", "extract": "e", "num_links": 1}],
      "signals": {"has_links": True, "blacklisted": True}}, "blocklist"),
])
def test_each_deterministic_check_fires(crawl, expected_kind):
    kinds = {item.kind for item in rules.crawl_evidence("example.com", crawl)}
    assert expected_kind in kinds


def test_do_not_crawl_list_is_decisive():
    items = rules.crawl_evidence("mwmbl.org", {"http_status": 200, "pages": [], "signals": {}})
    decisive = rules.decisive(items)
    assert decisive.kind == "do_not_crawl"
    assert decisive.implies_action == "REJECT"


def test_subdomain_redirect_is_not_treated_as_off_domain():
    items = rules.crawl_evidence(
        "example.com",
        {"http_status": 200, "final_domain": "www.example.com", "pages": [], "signals": {}})
    assert "redirect" not in {item.kind for item in items}


def test_first_time_submitter_is_called_out():
    items = rules.live_evidence({"approved": 0, "rejected": 0}, {})
    assert any("no track record" in item.label for item in items)


def test_previous_approval_of_the_same_domain_is_decisive():
    items = rules.live_evidence({"approved": 3, "rejected": 0}, {"approved": 1, "rejected": 0})
    assert rules.decisive(items).implies_action == "APPROVE"


# --------------------------------------------------------------------- suggestions

def test_a_deterministic_check_beats_the_model():
    """A 404 is not a matter of opinion, so no probability may override it."""
    model = mock.Mock()
    model.version = "test"
    model.predict.return_value = [(0.01, "SPAM", 0.9)]   # the model says "approve"

    items = rules.crawl_evidence(
        "example.com", {"http_status": 404, "pages": [], "signals": {}})
    suggestion = suggest("example.com", [], items, model=model)

    assert suggestion.action == "REJECT"
    assert suggestion.reason_source == "rule"
    model.predict.assert_not_called()


def test_a_check_that_implies_other_carries_the_detail_the_submitter_is_shown():
    """OTHER is the one reason that explains nothing on its own, so a suggestion carrying it
    has to carry the sentence too - the API refuses a decision that does not."""
    items = rules.crawl_evidence(
        "example.com", {"http_status": 404, "pages": [], "signals": {}})
    suggestion = suggest("example.com", [], items, model=mock.Mock(version="test"))

    assert (suggestion.action, suggestion.reason) == ("REJECT", "OTHER")
    assert suggestion.reason_detail == "Homepage returns HTTP 404"


def test_a_check_written_for_the_moderator_tells_the_submitter_something_readable():
    """A label is read by whoever is deciding the case and a detail by the person whose site
    was rejected, so a label naming the exception we caught is not the sentence to send."""
    items = rules.crawl_evidence(
        "example.com", {"error": "AbortError", "pages": [], "signals": {}})
    suggestion = suggest("example.com", [], items, model=mock.Mock(version="test"))

    assert (suggestion.action, suggestion.reason) == ("REJECT", "OTHER")
    assert suggestion.reason_detail == "We could not fetch this site when we tried to crawl it."
    # The moderator still gets the exception, in the evidence list where it belongs.
    assert "Could not be fetched (AbortError)" in [
        item["label"] for item in suggestion.evidence]


def test_the_do_not_crawl_list_explains_itself_from_the_submitters_side():
    """Its label is written from ours - "we don't crawl ourselves" - and is about our policy
    rather than about the site the submitter sent."""
    items = rules.crawl_evidence(
        "google.com", {"http_status": 200, "pages": [], "signals": {}})
    suggestion = suggest("google.com", [], items, model=mock.Mock(version="test"))

    assert (suggestion.action, suggestion.reason) == ("REJECT", "OTHER")
    assert suggestion.reason_detail == "We don't index search engines or our own site."


def test_a_reason_that_explains_itself_needs_no_detail():
    model = mock.Mock(version="test")
    model.predict.return_value = [(0.92, "SPAM", 0.8)]

    suggestion = suggest("example.com", [], [], model=model)

    assert (suggestion.action, suggestion.reason) == ("REJECT", "SPAM")
    assert suggestion.reason_detail == ""


@pytest.mark.parametrize("reason", ["OTHER", ""])
def test_the_model_does_not_suggest_a_rejection_it_cannot_give_the_reason_for(reason):
    """The reason head is trained on decisions and moderators do use OTHER, but nothing here
    can write the detail that has to go with it. A "Reject - other" button that fails
    validation when pressed is worse than no suggestion, so it goes back as UNSURE."""
    model = mock.Mock(version="test")
    model.predict.return_value = [(0.92, reason, 0.8)]

    suggestion = suggest("example.com", [], [], model=model)

    assert (suggestion.action, suggestion.reason, suggestion.reason_detail) == ("UNSURE", "", "")


def test_missing_model_degrades_to_unsure_not_to_a_default():
    with mock.patch("mwmbl.moderation.model.get_model", return_value=None):
        suggestion = suggest("example.com", ["some text"], [])
    assert suggestion.action == "UNSURE"
    assert suggestion.confidence == 0.0


def test_offensive_confidence_is_capped_because_it_has_no_real_labels():
    from mwmbl.moderation.model import REASON_CONFIDENCE_CAP
    assert REASON_CONFIDENCE_CAP["OFFENSIVE"] < 1.0


def test_review_priority_puts_confident_rejects_first_and_approvals_last():
    reject = Suggestion(action="REJECT", confidence=0.95)
    unsure = Suggestion(action="UNSURE", confidence=0.1)
    approve = Suggestion(action="APPROVE", confidence=0.95)
    assert reject.review_priority > unsure.review_priority > approve.review_priority


# --------------------------------------------------------------------- features

def test_featuriser_produces_the_same_width_for_unseen_input():
    featuriser = Featuriser()
    fitted = featuriser.fit_transform([
        ModerationExample("aianimegenerator.cloud", ["free ai anime generator"]),
        ModerationExample("docs.python.org", ["python language reference"]),
        ModerationExample("seobacklinkhub.org", ["best seo backlinks tool"]),
    ])
    transformed = featuriser.transform([ModerationExample("unseen.example", ["nothing alike"])])
    assert transformed.shape[1] == fitted.shape[1]
    assert len(featuriser.feature_names()) == fitted.shape[1]


def test_has_text_separates_an_uncrawled_domain_from_one_whose_words_are_unknown():
    """Both produce an all-zero TF-IDF row, and they mean opposite things. Evidence is crawled
    newest-first, so under a chronological split most training rows have no text and most test
    rows do; without this indicator the model cannot tell the two cases apart and the
    difference is absorbed into an intercept fitted on the training mix."""
    featuriser = Featuriser(use_has_text=True)
    featuriser.fit_transform([
        ModerationExample("aianimegenerator.cloud", ["free ai anime generator tool"]),
        ModerationExample("docs.python.org", ["python language reference tool"]),
        ModerationExample("seobacklinkhub.org", ["best seo backlinks tool"]),
    ])
    assert featuriser.text is not None
    column = list(featuriser.feature_names()).index("has_text")

    uncrawled = featuriser.transform([ModerationExample("a.example", [])])
    unmatched = featuriser.transform([ModerationExample("a.example", ["kanji zzz"])])
    assert uncrawled[0, column] == 0.0
    assert unmatched[0, column] == 1.0


def test_the_ablation_fits_the_same_features_minus_the_text_block():
    """--no-text has to run through the real featuriser, not a copy of it, or the ablation
    measures the copy."""
    examples = [ModerationExample("aianimegenerator.cloud", ["free ai anime generator tool"]),
                ModerationExample("docs.python.org", ["python language reference tool"]),
                ModerationExample("seobacklinkhub.org", ["best seo backlinks tool"])]
    with_text = Featuriser().fit_transform(examples).shape[1]
    without = Featuriser(use_text=False)
    assert without.fit_transform(examples).shape[1] < with_text
    assert without.text is None


def test_a_judges_most_confident_approval_is_not_read_as_a_certain_rejection(tmp_path):
    """scripts/moderation_llm_bakeoff accepts judgements on either a 0-1 or a 0-100 scale, and
    the scale has to be decided per file. Deciding per value reads a 1 out of 100 - the most
    confident approval a judge can express - as a probability of 1.0, the most confident
    rejection in the file. Both judges in the first bake-off emitted exactly one such row."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from moderation_llm_bakeoff import read_judgements

    path = tmp_path / "judge.judgements.jsonl"
    path.write_text(
        '{"domain": "obviously-fine.org", "reject_probability": 1, "reason": ""}\n'
        '{"domain": "aislopgenerator.xyz", "reject_probability": 95, "reason": "SPAM"}\n')

    judgements = read_judgements(path)
    assert judgements["obviously-fine.org"] == 0.01
    assert judgements["aislopgenerator.xyz"] == 0.95


def test_judgements_already_on_a_zero_to_one_scale_are_left_alone(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from moderation_llm_bakeoff import read_judgements

    path = tmp_path / "judge.judgements.jsonl"
    path.write_text(
        '{"domain": "obviously-fine.org", "reject_probability": 0.01, "reason": ""}\n'
        '{"domain": "aislopgenerator.xyz", "reject_probability": 0.95, "reason": "SPAM"}\n')

    assert read_judgements(path) == {"obviously-fine.org": 0.01, "aislopgenerator.xyz": 0.95}


def test_the_operating_point_is_measured_where_the_server_actually_runs():
    """PR-AUC summarises every threshold, including ones nothing runs at. What a moderator sees
    is decided by MODERATION_REJECT_THRESHOLD and MODERATION_APPROVE_THRESHOLD, and a model can
    hold its ranking while its scores drift through those."""
    truth = np.array([1, 1, 1, 0, 0, 0, 0, 0])
    scores = np.array([0.9, 0.8, 0.4, 0.9, 0.5, 0.1, 0.1, 0.1])

    point = operating_point(truth, scores)

    # 0.9, 0.8 and 0.9 clear the 0.75 reject threshold: two of the three are real rejections.
    assert point["reject_share"] == 3 / 8
    assert point["reject_precision"] == pytest.approx(2 / 3)
    assert point["reject_recall"] == pytest.approx(2 / 3)
    # 0.1, 0.1, 0.1 fall under the 0.25 approve threshold, and none of them was rejected.
    assert point["approve_share"] == 3 / 8
    assert point["approve_error_rate"] == 0.0
    assert point["unsure_share"] == 2 / 8


def test_a_threshold_that_selects_nothing_reports_null_not_zero():
    """A model that never suggests REJECT has no reject precision. Reporting 0.0 would read as
    a model that is wrong every time instead of one that never speaks."""
    truth = np.array([1, 1, 0, 0])
    point = operating_point(truth, np.array([0.5, 0.5, 0.5, 0.5]))

    assert point["reject_share"] == 0.0
    assert point["reject_precision"] is None
    assert point["approve_error_rate"] is None
    assert point["unsure_share"] == 1.0


def test_has_text_can_be_held_out_independently_of_the_text_block():
    """The two halves of the page-text features can fail independently - the vocabulary can
    earn its place while the indicator is a proxy for recency - so the ablation holds out each
    on its own rather than both together."""
    examples = [ModerationExample("aianimegenerator.cloud", ["free ai anime generator tool"]),
                ModerationExample("docs.python.org", ["python language reference tool"]),
                ModerationExample("seobacklinkhub.org", ["best seo backlinks tool"])]

    both = Featuriser(use_has_text=True)
    shipped = Featuriser()
    both.fit_transform(examples)
    shipped.fit_transform(examples)

    assert "has_text" in list(both.feature_names())
    # Off by default: measured against production data it made every metric worse.
    assert "has_text" not in list(shipped.feature_names())
    assert shipped.text is not None                 # the vocabulary block is still there
    assert shipped.transform(examples).shape[1] == both.transform(examples).shape[1] - 1


def test_malformed_historic_names_are_excluded_from_training():
    """Migrations 0032/0033 fixed these at the API layer; training on them teaches a
    problem that can no longer occur."""
    assert not is_trainable_domain("null")
    assert not is_trainable_domain("CapCutModsAPK.net")
    assert not is_trainable_domain("")
    assert is_trainable_domain("docs.python.org")


def test_derived_rows_never_reach_the_test_split():
    from mwmbl.moderation.train import split_by_time

    real = [TrainingRow(f"real{index}.com", index % 2 == 0, "", "real", [], f"2025-01-{index:02d}")
            for index in range(1, 21)]
    derived = [TrainingRow(f"derived{index}.com", True, "OFFENSIVE", "derived", [])
               for index in range(5)]
    seed = [TrainingRow("seed.com", True, "OTHER", "seed", [])]

    train_rows, test_rows = split_by_time(real + derived + seed)
    assert all(row.source == "real" for row in test_rows)
    assert {row.source for row in train_rows} == {"real", "derived", "seed"}


def test_agreement_is_measured_against_the_action_that_was_shown():
    """suggested_status records what the tool displayed - APPROVE, REJECT or UNSURE - so
    comparing it against a submission status ("REJECTED") matched nothing and inverted the
    count: every rejection the moderator agreed with was scored as a disagreement."""
    def decided(rejected, shown):
        return TrainingRow("example.com", rejected, "", "real", [], suggested_status=shown)

    influence = _suggestion_influence([
        decided(rejected=True, shown="REJECT"),      # agreed
        decided(rejected=False, shown="REJECT"),     # disagreed
        decided(rejected=False, shown="APPROVE"),    # agreed
        decided(rejected=True, shown="UNSURE"),      # shown, but took no side
        decided(rejected=True, shown=""),            # nothing was shown
    ])

    assert influence == {"real_rows": 5, "shown_a_suggestion": 4,
                         "suggested_a_side": 3, "agreed_with_it": 2}


# --------------------------------------------------------------------- API

@pytest.mark.django_db
def test_queue_requires_the_moderator_permission(client, submitter):
    response = client.get(QUEUE_URL, headers={"Authorization": token(submitter)})
    assert response.status_code == 403


@pytest.mark.django_db
def test_queue_runs_no_model(client, moderator, submitter):
    """The whole point of precomputing: a moderator's request never waits on inference."""
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=submitter)
    ready_evidence(submission.name)

    with mock.patch("mwmbl.moderation.model.get_model") as get_model:
        response = client.get(QUEUE_URL, headers={"Authorization": token(moderator)})

    assert response.status_code == 200
    get_model.assert_not_called()


@pytest.mark.django_db
def test_uncrawled_submission_reports_that_rather_than_guessing(client, moderator, submitter):
    DomainSubmission.objects.create(name="not-yet-crawled.com", submitted_by=submitter)

    response = client.get(QUEUE_URL, headers={"Authorization": token(moderator)})
    item = response.json()["items"][0]

    assert item["evidence_state"] == "PENDING"
    assert item["suggestion"] is None


@pytest.mark.django_db
def test_queue_orders_rows_that_need_a_human_first(client, moderator, established):
    for name, action in [("approve-me.com", "APPROVE"),
                         ("reject-me.com", "REJECT"),
                         ("unsure.com", "UNSURE")]:
        DomainSubmission.objects.create(name=name, submitted_by=established)
        ready_evidence(name, suggested_action=action, confidence=0.8)

    response = client.get(f"{QUEUE_URL}?order_by=needs_review",
                          headers={"Authorization": token(moderator)})
    assert [item["name"] for item in response.json()["items"]] == [
        "reject-me.com", "unsure.com", "approve-me.com"]


@pytest.mark.django_db
def test_queue_filters_on_the_suggestion(client, moderator, submitter):
    for name, action in [("a.com", "APPROVE"), ("b.com", "REJECT")]:
        DomainSubmission.objects.create(name=name, submitted_by=submitter)
        ready_evidence(name, suggested_action=action)

    response = client.get(f"{QUEUE_URL}?suggested_action=REJECT",
                          headers={"Authorization": token(moderator)})
    assert [item["name"] for item in response.json()["items"]] == ["b.com"]


@pytest.mark.django_db
def test_decision_records_the_suggestion_that_was_shown(client, moderator, submitter):
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        f"/api/v1/platform/domain-submissions/ids/{submission.id}",
        data={"status": "REJECTED", "rejection_reason": "SPAM", "rejection_detail": "",
              "suggested_status": "APPROVE", "suggested_reason": "",
              "suggestion_confidence": 0.62, "suggestion_model_version": "test-model"},
        content_type="application/json", headers={"Authorization": token(moderator)})
    assert response.status_code == 200

    submission.refresh_from_db()
    assert submission.status == "REJECTED"
    # The moderator disagreed with the tool, and that disagreement is what makes the next
    # retrain able to tell whether the suggestions are helping.
    assert submission.suggested_status == "APPROVE"
    assert submission.suggestion_confidence == pytest.approx(0.62)
    assert submission.status_changed_by == moderator


@pytest.mark.django_db
def test_bulk_decisions_apply_each_choice_separately(client, moderator, submitter):
    first = DomainSubmission.objects.create(name="a.com", submitted_by=submitter)
    second = DomainSubmission.objects.create(name="b.com", submitted_by=submitter)

    response = client.post(
        "/api/v1/platform/domain-submissions/decisions",
        data={"decisions": [
            {"domain": "a.com", "status": "APPROVED"},
            {"domain": "b.com", "status": "REJECTED", "rejection_reason": "SPAM"},
        ]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.json()["updated"] == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.status, second.status) == ("APPROVED", "REJECTED")


@pytest.mark.django_db
def test_refetch_clears_the_evidence_so_the_crawl_actually_reruns(client, moderator, submitter):
    submission = DomainSubmission.objects.create(name="was-down.com", submitted_by=submitter)
    ready_evidence(submission.name)

    with mock.patch("mwmbl.platform.api.enrich_domain_submission") as enrich:
        response = client.post(
            f"/api/v1/platform/domain-submissions/ids/{submission.id}/refetch",
            headers={"Authorization": token(moderator)})

    assert response.status_code == 200
    assert not DomainEvidence.objects.filter(domain="was-down.com").exists()
    enrich.assert_called_once_with("was-down.com")


# ------------------------------------------------------------ the queue is per domain

DECISIONS_URL = "/api/v1/platform/domain-submissions/decisions"
HISTORY_URL = "/api/v1/platform/domain-submissions/moderated"


def upvote(url, username):
    SearchResultVote.objects.create(
        user=MwmblUser.objects.create_user(username=username), url=url, query="q",
        vote_type="upvote")


def downvote(url, username):
    SearchResultVote.objects.create(
        user=MwmblUser.objects.create_user(username=username), url=url, query="q",
        vote_type="downvote")


@pytest.mark.django_db
def test_a_domain_submitted_nine_times_is_one_row(client, moderator, established):
    """A moderator reviews a domain, not a submission. Nine asks for the same site are one
    card and one decision - the old queue made them nine of each."""
    for _ in range(9):
        DomainSubmission.objects.create(name="cheap-rolex.biz", submitted_by=established)
    DomainSubmission.objects.create(name="solarpunk.zone", submitted_by=established)
    ready_evidence("cheap-rolex.biz")
    ready_evidence("solarpunk.zone")

    body = client.get(QUEUE_URL, headers={"Authorization": token(moderator)}).json()

    assert body["count"] == 2
    counts = {item["name"]: item["submission_count"] for item in body["items"]}
    assert counts == {"cheap-rolex.biz": 9, "solarpunk.zone": 1}


@pytest.mark.django_db
def test_the_row_is_the_first_submission_of_the_domain(client, moderator):
    """The card says "first submitted 6 days ago by anon_4417", so the row we deduplicate to
    has to be the row those two fields come from."""
    first = MwmblUser.objects.create_user(username="anon_4417")
    later = MwmblUser.objects.create_user(username="someone_else")
    DomainSubmission.objects.create(name="a.com", submitted_by=first,
                                    submitted_on=timezone.now() - timedelta(days=6))
    DomainSubmission.objects.create(name="a.com", submitted_by=later,
                                    submitted_on=timezone.now() - timedelta(days=1))
    ready_evidence("a.com")

    item = client.get(QUEUE_URL, headers={"Authorization": token(moderator)}).json()["items"][0]

    assert item["first_submitted_by_username"] == "anon_4417"
    assert item["first_submitted_by"] == first.id


@pytest.mark.django_db
def test_votes_are_rolled_up_to_the_domain(client, moderator, established):
    """The card shows the domain's score, so votes on any of its URLs count - including ones
    cast against www., which is how half the index refers to the same site."""
    DomainSubmission.objects.create(name="solarpunk.zone", submitted_by=established)
    ready_evidence("solarpunk.zone")
    upvote("https://solarpunk.zone/notes/co-op", "voter1")
    upvote("https://www.solarpunk.zone/repair-cafes", "voter2")
    downvote("https://solarpunk.zone/about", "voter3")
    upvote("https://somewhere-else.com/", "voter4")

    DomainSubmission.objects.create(name="silent.example", submitted_by=established)
    ready_evidence("silent.example")

    items = {item["name"]: item
             for item in client.get(QUEUE_URL,
                                    headers={"Authorization": token(moderator)}).json()["items"]}

    assert (items["solarpunk.zone"]["upvotes"], items["solarpunk.zone"]["downvotes"]) == (2, 1)
    # Zero, not null: a domain nobody has voted on has to sort with the rest.
    assert (items["silent.example"]["upvotes"], items["silent.example"]["downvotes"]) == (0, 0)


@pytest.mark.django_db
def test_a_www_submission_still_finds_its_votes(client, moderator, established):
    DomainSubmission.objects.create(name="www.example.com", submitted_by=established)
    ready_evidence("www.example.com")
    upvote("https://example.com/a", "voter1")

    item = client.get(QUEUE_URL, headers={"Authorization": token(moderator)}).json()["items"][0]
    assert item["upvotes"] == 1


@pytest.mark.django_db
def test_the_default_order_is_most_submitted_then_most_upvoted(client, moderator, established):
    for name, submissions, upvotes in [("popular.com", 3, 1), ("tied-low.com", 2, 1),
                                       ("tied-high.com", 2, 40), ("lonely.com", 1, 99)]:
        for _ in range(submissions):
            DomainSubmission.objects.create(name=name, submitted_by=established)
        for index in range(upvotes):
            upvote(f"https://{name}/{index}", f"voter-{name}-{index}")
        ready_evidence(name)

    body = client.get(QUEUE_URL, headers={"Authorization": token(moderator)}).json()
    assert [item["name"] for item in body["items"]] == [
        "popular.com", "tied-high.com", "tied-low.com", "lonely.com"]


@pytest.mark.django_db
def test_the_card_carries_its_sample_pages_and_padlock(client, moderator, established):
    """Everything a card draws travels with the row, so advancing to the next one costs no
    request."""
    DomainSubmission.objects.create(name="crawled.com", submitted_by=established)
    ready_evidence("crawled.com", signals={"has_links": True, "https": False})
    DomainSubmission.objects.create(name="uncrawled.com", submitted_by=established)
    DomainSubmission.objects.create(name="unreachable.com", submitted_by=established)
    ready_evidence("unreachable.com", signals={"has_links": False, "https": None},
                   error="AbortError", http_status=None)

    items = {item["name"]: item
             for item in client.get(QUEUE_URL,
                                    headers={"Authorization": token(moderator)}).json()["items"]}

    assert items["crawled.com"]["pages"][0]["title"] == "A title"
    assert items["crawled.com"]["https"] is False
    # Not False: an uncrawled domain must not draw an open padlock.
    assert items["uncrawled.com"]["https"] is None
    assert items["uncrawled.com"]["pages"] == []
    # Nor a domain that was crawled and never answered - nothing was learned about its TLS.
    assert items["unreachable.com"]["https"] is None


@pytest.mark.django_db
def test_one_decision_settles_every_submission_of_the_domain(client, moderator, submitter):
    for _ in range(9):
        DomainSubmission.objects.create(name="cheap-rolex.biz", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "cheap-rolex.biz", "status": "REJECTED",
                             "rejection_reason": "SPAM"}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.json() == {"status": "ok", "domains": 1, "updated": 9, "not_found": []}
    assert not DomainSubmission.objects.filter(name="cheap-rolex.biz",
                                               status="PENDING").exists()


@pytest.mark.django_db
def test_a_decision_can_be_changed_after_the_fact(client, moderator, submitter):
    """Re-deciding is the same request as deciding: a domain already rejected can be approved
    without a second endpoint, and does not end up half one and half the other."""
    DomainSubmission.objects.create(name="a.com", submitted_by=submitter, status="REJECTED",
                                    rejection_reason="SPAM")
    DomainSubmission.objects.create(name="a.com", submitted_by=submitter)

    client.post(DECISIONS_URL,
                data={"decisions": [{"domain": "a.com", "status": "APPROVED"}]},
                content_type="application/json",
                headers={"Authorization": token(moderator)})

    statuses = set(DomainSubmission.objects.filter(name="a.com")
                   .values_list("status", "rejection_reason"))
    assert statuses == {("APPROVED", "")}


@pytest.mark.django_db
def test_a_decision_on_an_unknown_domain_is_reported_not_invented(client, moderator):
    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "never-submitted.com", "status": "APPROVED"}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.json()["not_found"] == ["never-submitted.com"]


# ------------------------------------------------------------------- undo and history

UNDO_URL = "/api/v1/platform/domain-submissions/domains/{}/undo"


@pytest.mark.django_db
def test_undo_returns_a_domain_to_the_queue_without_rewriting_the_audit(client, moderator,
                                                                       submitter):
    """The suggested_* columns record what was on screen when the decision was made. Posting
    a PENDING status back through the decision endpoint - the nearest thing to an undo before
    this - overwrote them from the request, destroying the one thing they exist for."""
    submission = DomainSubmission.objects.create(name="a.com", submitted_by=submitter)
    ready_evidence("a.com")
    client.post(DECISIONS_URL,
                data={"decisions": [{"domain": "a.com", "status": "REJECTED",
                                     "rejection_reason": "SPAM",
                                     "suggested_status": "REJECT",
                                     "suggested_reason": "SPAM",
                                     "suggestion_confidence": 0.91,
                                     "suggestion_model_version": "2026-08-01"}]},
                content_type="application/json", headers={"Authorization": token(moderator)})

    response = client.post(UNDO_URL.format("a.com"),
                           headers={"Authorization": token(moderator)})

    assert response.status_code == 200
    submission.refresh_from_db()
    assert (submission.status, submission.rejection_reason) == ("PENDING", "")
    assert (submission.suggested_status, submission.suggested_reason) == ("REJECT", "SPAM")
    assert submission.suggestion_confidence == 0.91
    assert submission.suggestion_model_version == "2026-08-01"

    body = client.get(QUEUE_URL, headers={"Authorization": token(moderator)}).json()
    assert [item["name"] for item in body["items"]] == ["a.com"]


@pytest.mark.django_db
def test_undoing_an_approval_rebuilds_the_blacklist_snapshot(client, moderator, submitter):
    """An approved domain is subtracted from the remote blocklists when the snapshot is built,
    so it stays subtracted until the snapshot is rebuilt. .update() fires no post_save, so the
    approval receiver never runs and the undo has to ask for the rebuild itself."""
    DomainSubmission.objects.create(name="a.com", submitted_by=submitter, status="APPROVED")
    # The approval itself scheduled one, and the debounce would rightly decline a second.
    # Clearing it is the run having already happened, which is the case the undo is about.
    Task.objects.all().delete()

    with mock.patch("mwmbl.background.refresh_blacklist_snapshot") as refresh:
        client.post(UNDO_URL.format("a.com"), headers={"Authorization": token(moderator)})

    refresh.assert_called_once()


@pytest.mark.django_db
def test_undoing_a_rejection_does_not_rebuild_the_snapshot(client, moderator, submitter):
    """Only an approval ever changed the snapshot, so only undoing one has to change it back -
    a rebuild downloads and parses tens of megabytes."""
    DomainSubmission.objects.create(name="a.com", submitted_by=submitter, status="REJECTED",
                                    rejection_reason="SPAM")
    Task.objects.all().delete()

    with mock.patch("mwmbl.background.refresh_blacklist_snapshot") as refresh:
        client.post(UNDO_URL.format("a.com"), headers={"Authorization": token(moderator)})

    refresh.assert_not_called()


@pytest.mark.django_db
def test_undoing_a_domain_nobody_submitted_is_a_404(client, moderator):
    response = client.post(UNDO_URL.format("never-submitted.com"),
                           headers={"Authorization": token(moderator)})
    assert response.status_code == 404


@pytest.mark.django_db
def test_history_lists_past_decisions_by_status_newest_first(client, moderator, submitter):
    for name, status, when in [("old-reject.com", "REJECTED", 5),
                               ("new-reject.com", "REJECTED", 1),
                               ("approved.com", "APPROVED", 2)]:
        DomainSubmission.objects.create(
            name=name, submitted_by=submitter, status=status,
            status_changed_by=moderator, status_changed_on=timezone.now() - timedelta(days=when))

    body = client.get(f"{HISTORY_URL}?status=REJECTED",
                      headers={"Authorization": token(moderator)}).json()

    assert [item["name"] for item in body["items"]] == ["new-reject.com", "old-reject.com"]
    assert body["count"] == 2
    assert body["items"][0]["status_changed_by_username"] == "moderator"


@pytest.mark.django_db
def test_history_shows_one_row_per_domain(client, moderator, submitter):
    for index in range(4):
        DomainSubmission.objects.create(
            name="a.com", submitted_by=submitter, status="REJECTED", rejection_reason="SPAM",
            status_changed_by=moderator,
            status_changed_on=timezone.now() - timedelta(days=index))

    body = client.get(HISTORY_URL, headers={"Authorization": token(moderator)}).json()

    assert body["count"] == 1
    assert body["items"][0]["submission_count"] == 4


@pytest.mark.django_db
def test_history_carries_what_the_moderator_was_shown(client, moderator, submitter):
    DomainSubmission.objects.create(
        name="a.com", submitted_by=submitter, status="REJECTED", rejection_reason="SPAM",
        status_changed_by=moderator, status_changed_on=timezone.now(),
        suggested_status="REJECT", suggested_reason="SPAM", suggestion_confidence=0.77,
        suggestion_model_version="2026-07-01")

    item = client.get(HISTORY_URL,
                      headers={"Authorization": token(moderator)}).json()["items"][0]

    assert item["suggested_status"] == "REJECT"
    assert item["suggestion_confidence"] == 0.77
    assert item["suggestion_model_version"] == "2026-07-01"


@pytest.mark.django_db
def test_history_requires_the_moderator_permission(client, submitter):
    response = client.get(HISTORY_URL, headers={"Authorization": token(submitter)})
    assert response.status_code == 403


# --------------------------------------------------------------------- rescoring

@pytest.mark.django_db
def test_rescore_updates_pending_rows_only(submitter):
    from mwmbl.background import rescore_pending_submissions

    DomainSubmission.objects.create(name="pending.com", submitted_by=submitter, status="PENDING")
    decided = DomainSubmission.objects.create(
        name="decided.com", submitted_by=submitter, status="APPROVED",
        suggested_status="REJECT", suggestion_confidence=0.9)
    ready_evidence("pending.com", model_version="old")
    ready_evidence("decided.com", model_version="old")

    with mock.patch("mwmbl.moderation.model.get_model", return_value=None):
        rescore_pending_submissions.now()

    assert DomainEvidence.objects.get(domain="pending.com").model_version == ""
    assert DomainEvidence.objects.get(domain="decided.com").model_version == "old"
    decided.refresh_from_db()
    # The audit of what was on screen when this was decided must survive a retrain.
    assert decided.suggested_status == "REJECT"


@pytest.mark.django_db
def test_enrichment_skips_a_domain_whose_evidence_is_still_fresh(submitter):
    from mwmbl.background import enrich_domain_submission

    ready_evidence("fresh.com")
    with mock.patch("mwmbl.background.crawl_domain") as crawl:
        enrich_domain_submission.now("fresh.com")
    crawl.assert_not_called()


@pytest.mark.django_db
def test_enrichment_recrawls_stale_evidence(submitter):
    from mwmbl.background import enrich_domain_submission

    ready_evidence("stale.com", fetched_at=timezone.now() - timedelta(days=365))
    with mock.patch("mwmbl.background.crawl_domain") as crawl:
        crawl.return_value = {"http_status": 200, "final_domain": "", "error": "",
                              "pages": [], "signals": {}}
        enrich_domain_submission.now("stale.com")
    crawl.assert_called_once()


@pytest.mark.django_db
def test_queue_cost_does_not_grow_with_the_backlog(client, moderator, established,
                                                   django_assert_max_num_queries):
    """Ordering, filtering and slicing must happen in SQL.

    The backlog is ~4,000 pending submissions. An implementation that builds the list in
    Python and sorts it there pays for all of them on every request, and issues per-row
    queries for the submitter and prior-decision checks on top. Both are invisible on a small
    test fixture, so this pins the query count instead: it must be flat in the number of rows.
    """
    for index in range(30):
        name = f"site{index}.example"
        # Several submissions and several votes per domain, so that grouping to one row per
        # domain and rolling the votes up cannot quietly become a query per row either.
        for _ in range(3):
            DomainSubmission.objects.create(name=name, submitted_by=established)
        for voter in range(index % 4):
            SearchResultVote.objects.create(
                user=MwmblUser.objects.create_user(username=f"voter{index}-{voter}"),
                url=f"https://{name}/page{voter}", query="q", vote_type="upvote")
        ready_evidence(name, suggested_action="REJECT", confidence=index / 30)

    # Auth, permissions, count, page, evidence, submitter counts, prior-decision counts.
    with django_assert_max_num_queries(12):
        response = client.get(f"{QUEUE_URL}?limit=5&order_by=needs_review",
                              headers={"Authorization": token(moderator)})

    body = response.json()
    # Distinct pending domains, not the 90 pending submissions behind them.
    assert body["count"] == 30
    assert len(body["items"]) == 5
    # ...and the page is the top of the global ordering, not the top of an arbitrary slice.
    assert body["items"][0]["name"] == "site29.example"
    assert body["items"][0]["submission_count"] == 3
    assert body["items"][0]["upvotes"] == 1


@pytest.mark.django_db
def test_queue_paginates_over_the_whole_ordering(client, moderator, established):
    for index in range(10):
        name = f"site{index}.example"
        DomainSubmission.objects.create(name=name, submitted_by=established)
        ready_evidence(name, suggested_action="REJECT", confidence=index / 10)

    def page(offset):
        response = client.get(f"{QUEUE_URL}?limit=4&offset={offset}&order_by=needs_review",
                              headers={"Authorization": token(moderator)})
        return [item["name"] for item in response.json()["items"]]

    assert page(0) + page(4) + page(8) == [f"site{index}.example"
                                           for index in range(9, -1, -1)]


def test_robots_denied_is_evidence_not_a_verdict():
    """lobste.rs disallows `User-agent: *` and was approved anyway, and not one of the 76
    distinct rejection details mentions robots.txt. Suggesting a confident rejection here
    would be confidently wrong on a domain moderators actually wanted."""
    items = rules.crawl_evidence(
        "lobste.rs", {"http_status": None, "error": "RobotsDenied", "pages": [{}],
                      "signals": {}})
    robots = next(item for item in items if item.kind == "robots")
    assert robots.implies_action is None
    assert rules.decisive(items) is None


def test_blocklist_membership_is_evidence_not_a_verdict():
    """Overriding a false positive on these lists is exactly what an approval is for -
    mwmbl.curated_domains names pudding.cool and contactmusic.com as examples - so the tool
    must not argue against the mechanism it serves."""
    items = rules.crawl_evidence(
        "pudding.cool",
        {"http_status": 200, "pages": [{"title": "t", "extract": "e", "num_links": 4}],
         "signals": {"has_links": True, "blacklisted": True}})
    blocklist = next(item for item in items if item.kind == "blocklist")
    assert blocklist.direction == rules.REJECT
    assert blocklist.implies_action is None


def test_a_failed_fetch_reports_one_problem_not_four():
    items = rules.crawl_evidence(
        "gone.example",
        {"http_status": None, "error": "AbortError", "pages": [{"title": "", "extract": "",
                                                                "num_links": 0}],
         "signals": {}})
    kinds = {item.kind for item in items}
    assert kinds == {"unreachable"}


def test_an_approval_is_not_labelled_with_the_reason_heads_argmax():
    model = mock.Mock()
    model.version = "test"
    model.predict.return_value = [(0.02, "OFFENSIVE", 0.5)]   # clearly an approval

    suggestion = suggest("example.com", ["wholesome text"], [], model=model)

    assert suggestion.action == "APPROVE"
    assert suggestion.reason == ""
    assert suggestion.reason_source == "model"


def test_redirect_check_errs_towards_reporting_nothing():
    """The redirect item is decisive, so it must never fire on a site moving between its own
    subdomains. The cost is missing a genuine cross-site redirect under a multi-part suffix
    like .co.uk, which is a lost evidence line rather than a wrong rejection."""
    assert rules.registrable("www.example.com") == rules.registrable("docs.example.com")
    assert rules.registrable("example.co.uk") == rules.registrable("somewhere-else.co.uk")

    items = rules.crawl_evidence(
        "example.com",
        {"http_status": 200, "final_domain": "elsewhere.net", "pages": [], "signals": {}})
    assert rules.decisive(items).kind == "redirect"


# --------------------------------------------------------------------- retrain gate

def metrics(pr_auc, low, high, rows=150, positives=81):
    """Cold-start metrics in the shape a stored artifact carries.

    rows and positives are what make the PR-AUC readable: the gate corrects for prevalence, so
    a slice cannot be described by its score alone.
    """
    return {"cold_start": {"pr_auc": pr_auc, "pr_auc_ci": [low, high],
                           "rows": rows, "positives": positives}}


def paired(difference, low, high, contamination_known=True):
    return {"cold_start": metrics(0.7, 0.6, 0.8)["cold_start"],
            "versus_incumbent": {
                "incumbent_version": "domain-mod-2026-08-01", "rows": 150,
                "contamination_known": contamination_known, "difference": difference,
                "difference_ci": [low, high], "candidate_wins_fraction": 0.5}}


def test_gate_publishes_when_there_is_no_incumbent():
    allowed, _ = passes_gate(metrics(0.78, 0.69, 0.86), {})
    assert allowed


def test_gate_blocks_a_model_below_the_incumbents_lower_bound():
    allowed, explanation = passes_gate(metrics(0.60, 0.51, 0.69),
                                       metrics(0.78, 0.69, 0.86))
    assert not allowed
    assert "unpaired" in explanation


def test_gate_tolerates_movement_inside_the_interval():
    """The measured augmentation experiment moved the point estimate by 0.04 while the
    intervals spanned 0.17. A gate on point estimates would treat that noise as signal in
    both directions - blocking good models and shipping bad ones."""
    allowed, _ = passes_gate(metrics(0.74, 0.65, 0.83), metrics(0.78, 0.69, 0.86))
    assert allowed


def test_gate_is_not_fooled_by_a_change_in_prevalence():
    """The August 2026 retrain, exactly as it happened.

    Migration 0037 repaired 1,785 previously-invisible decisions into the training set, which
    moved the cold-start rejection rate from 54% to 30%. PR-AUC's floor *is* the positive rate,
    so the same quality of ranking scored 0.672 instead of 0.779 and the gate called it a
    regression. Corrected for prevalence it is a small improvement, 0.519 -> 0.532, and it
    ships."""
    allowed, explanation = passes_gate(
        metrics(0.6716, 0.5927, 0.7472, rows=635, positives=189),
        metrics(0.7788, 0.6885, 0.8588, rows=150, positives=81))
    assert allowed, explanation


def test_the_paired_comparison_is_preferred_to_the_stored_metrics():
    """Two numbers from two different test sets can only be compared weakly. When the incumbent
    could be loaded and scored on the same rows, that is the answer - even when the stored
    metrics, describing a different population, would have said otherwise."""
    allowed, explanation = passes_gate(paired(0.02, -0.03, 0.07),
                                       metrics(0.95, 0.93, 0.97))
    assert allowed
    assert "paired against domain-mod-2026-08-01" in explanation


def test_gate_blocks_a_candidate_that_is_confidently_worse():
    allowed, explanation = passes_gate(paired(-0.18, -0.26, -0.09), metrics(0.5, 0.4, 0.6))
    assert not allowed
    assert "tolerance" in explanation


def test_gate_ships_a_small_loss_it_cannot_distinguish_from_noise():
    """Non-inferiority, not superiority. Requiring a significant *gain* on a slice with ~80
    positives would never ship anything, including the models that are genuinely better."""
    allowed, _ = passes_gate(paired(-0.02, -0.045, 0.01), metrics(0.5, 0.4, 0.6))
    assert allowed


def test_an_incumbent_with_no_training_record_is_flagged_not_trusted_silently():
    """Artifacts pickled before train_domains was recorded may be scored on rows they were
    fitted on, which flatters them. The candidate still has to clear the bar - the point is
    that the explanation says the bar was tilted."""
    allowed, explanation = passes_gate(paired(0.01, -0.02, 0.05, contamination_known=False),
                                       metrics(0.5, 0.4, 0.6))
    assert allowed
    assert "no record of its training domains" in explanation


def test_gate_refuses_when_there_is_no_cold_start_slice():
    """Too little held-out data to judge is a reason not to publish, not a free pass."""
    allowed, explanation = passes_gate({"all": {"pr_auc": 0.9}}, {})
    assert not allowed
    assert "cold-start" in explanation


# --------------------------------------------------------------------- prior shift

@pytest.mark.django_db
def test_no_confident_approval_for_a_submitter_with_no_track_record(submitter):
    """The model is trained on a population that rejects 11% and asked about one that rejects
    54%, so a low reject score means much less for a first-time submitter - measured at 44%
    wrong. Withhold the approval rather than present it at face value."""
    submission = DomainSubmission.objects.create(name="unknown.example", submitted_by=submitter)
    evidence = ready_evidence("unknown.example", suggested_action="APPROVE", confidence=0.9)

    suggestion = suggestion_for(submission, evidence)

    assert suggestion.action == "UNSURE"


@pytest.mark.django_db
def test_approval_stands_for_a_submitter_with_a_record(submitter):
    for index in range(3):
        DomainSubmission.objects.create(name=f"past{index}.example", submitted_by=submitter,
                                        status="APPROVED")
    submission = DomainSubmission.objects.create(name="known.example", submitted_by=submitter)
    evidence = ready_evidence("known.example", suggested_action="APPROVE", confidence=0.9)

    suggestion = suggestion_for(submission, evidence)

    assert suggestion.action == "APPROVE"
    assert suggestion.confidence == pytest.approx(0.9)


@pytest.mark.django_db
def test_rejections_are_not_withheld_from_first_time_submitters(submitter):
    """Rejection precision is 0.88 on exactly this slice, so only the approve side is held."""
    submission = DomainSubmission.objects.create(name="spammy.example", submitted_by=submitter)
    evidence = ready_evidence("spammy.example", suggested_action="REJECT",
                              suggested_reason="SPAM", confidence=0.9)

    suggestion = suggestion_for(submission, evidence)

    assert suggestion.action == "REJECT"
    assert suggestion.reason == "SPAM"


# --------------------------------------------------------------------- crawl evidence

class _FakeResponse:
    """Enough of a requests Response for retrieve.fetch."""

    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = 300 <= status_code < 400
        self.next = object() if self.is_redirect else None
        self._body = body

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


A_PAGE = (b"<html><head><title>Buy this domain</title></head><body>"
          b"<p>This premium domain name is for sale. Make an offer today and we will be in "
          b"touch with you shortly to complete the transfer.</p></body></html>")


def crawl_with(responses: dict, domain: str) -> dict:
    """Crawl ``domain`` against canned HTTP responses, through the real crawler.

    Driven from requests.get rather than by stubbing crawl_url, because everything this
    covers lives in the plumbing between the fetch and the evidence.
    """
    def get(url, **kwargs):
        response = responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    blacklist = mock.Mock(is_domain_blacklisted=mock.Mock(return_value=False))
    with mock.patch("mwmbl.crawler.retrieve.requests.get", get), \
            mock.patch("mwmbl.crawler.retrieve.validate_url"), \
            mock.patch("mwmbl.crawler.retrieve.robots_allowed", return_value=True), \
            mock.patch("mwmbl.moderation.evidence.get_snapshot_blacklist",
                       return_value=blacklist):
        return crawl_domain(domain, redis=None)


def test_a_domain_that_redirects_elsewhere_is_reported_as_a_redirect():
    """Roughly a third of the rejection details moderators write are about dead or squatted
    domains, and "redirects to somewhere else" is the fetch that proves it. The check is only
    as good as the URL the crawler reports: reporting the requested one compares the domain
    against itself and the rule can never fire."""
    crawl = crawl_with({
        "https://squatted.example/": _FakeResponse(
            301, headers={"Location": "https://parked.test/lander"}),
        "https://parked.test/lander": _FakeResponse(200, body=A_PAGE),
    }, "squatted.example")

    assert crawl["final_domain"] == "parked.test"
    decisive = rules.decisive(rules.crawl_evidence("squatted.example", crawl))
    assert decisive.kind == "redirect"
    assert decisive.implies_action == "REJECT"


def test_a_domain_that_serves_its_own_pages_reports_no_redirect():
    """The other direction, because the check is decisive: a site that answers for itself must
    never be rejected for redirecting to itself."""
    crawl = crawl_with(
        {"https://honest.example/": _FakeResponse(200, body=A_PAGE)}, "honest.example")

    assert crawl["final_domain"] == ""
    kinds = {item.kind for item in rules.crawl_evidence("honest.example", crawl)}
    assert "redirect" not in kinds


def test_a_site_without_a_certificate_is_a_site_not_a_dead_domain():
    """Only https used to be tried, so "no TLS" and "not there" were the same result - and
    that result is a decisive REJECT for being unreachable. A site served over plain http is
    a site; whether it has a certificate is a line for the moderator, not a verdict."""
    crawl = crawl_with({
        "https://no-cert.example/": ConnectionError("certificate verify failed"),
        "http://no-cert.example/": _FakeResponse(200, body=A_PAGE),
    }, "no-cert.example")

    assert crawl["signals"]["https"] is False
    assert crawl["error"] == ""
    items = {item.kind: item for item in rules.crawl_evidence("no-cert.example", crawl)}
    assert items["no_tls"].direction == rules.NEUTRAL
    assert rules.decisive(items.values()) is None


def test_a_domain_that_is_simply_down_is_still_reported_as_unreachable():
    """The other direction: falling back to http must not turn a dead domain into a live one,
    and must not label it "no TLS" - the https attempt is the one that describes production."""
    crawl = crawl_with({
        "https://gone.example/": ConnectionError("no route to host"),
        "http://gone.example/": ConnectionError("no route to host"),
    }, "gone.example")

    # Unknown, not absent: the rules layer already gates "no TLS" behind having reached the
    # site, but the queue card reads this signal straight out of the row, and False there
    # draws a definite "no TLS" for a domain nothing ever answered for.
    assert crawl["signals"]["https"] is None
    assert crawl["error"] == "AbortError"
    kinds = {item.kind for item in rules.crawl_evidence("gone.example", crawl)}
    assert "no_tls" not in kinds
    assert rules.decisive(rules.crawl_evidence("gone.example", crawl)).kind == "unreachable"


def test_a_normal_https_site_is_not_asked_for_over_http():
    crawl = crawl_with(
        {"https://secure.example/": _FakeResponse(200, body=A_PAGE)}, "secure.example")

    assert crawl["signals"]["https"] is True
    assert "no_tls" not in {item.kind
                            for item in rules.crawl_evidence("secure.example", crawl)}


# --------------------------------------------------------------------- enrichment

@pytest.mark.django_db
def test_enrichment_never_commits_a_scored_row_without_the_score():
    """READY with a fresh timestamp is what makes the task skip a domain for a month. A row
    that reached it without a suggestion - a worker restarted between the two writes - would
    tell moderators "not assessed yet" until the evidence expired."""
    from mwmbl.background import enrich_domain_submission

    crawl = {"http_status": 200, "final_domain": "", "error": "", "pages": [], "signals": {}}
    with mock.patch("mwmbl.background.crawl_domain", return_value=crawl), \
            mock.patch("mwmbl.background.refresh_suggestion",
                       side_effect=RuntimeError("scoring blew up")):
        with pytest.raises(RuntimeError):
            enrich_domain_submission.now("half-written.example")

    assert not DomainEvidence.objects.filter(domain="half-written.example").exists()


@pytest.mark.django_db
def test_a_failed_recrawl_does_not_leave_the_old_crawl_on_screen():
    """The detail panel renders whatever is on the row, so a month-old crawl next to a FAILED
    state reads as current evidence about a site that is in fact unreachable."""
    from mwmbl.background import enrich_domain_submission

    ready_evidence("gone.example", fetched_at=timezone.now() - timedelta(days=365))
    with mock.patch("mwmbl.background.crawl_domain",
                    side_effect=ConnectionError("no route to host")):
        enrich_domain_submission.now("gone.example")

    evidence = DomainEvidence.objects.get(domain="gone.example")
    assert evidence.state == DomainEvidence.State.FAILED
    assert evidence.error == "ConnectionError"
    assert evidence.pages == []
    assert evidence.suggested_action == ""
    assert evidence.confidence is None


@pytest.mark.django_db
def test_the_reason_is_shown_with_its_own_confidence(submitter):
    """A model can be sure a domain should go and barely have a view on why. Showing the
    rejection's confidence next to the reason presents a guess as a finding."""
    submission = DomainSubmission.objects.create(name="spam.example", submitted_by=submitter)
    model = mock.Mock(version="test-model")
    model.predict.return_value = [(0.92, "SPAM", 0.31)]

    with mock.patch("mwmbl.moderation.model.get_model", return_value=model):
        refresh_suggestion(ready_evidence("spam.example"))

    evidence = DomainEvidence.objects.get(domain="spam.example")
    assert evidence.confidence == pytest.approx(0.92)
    assert evidence.reason_confidence == pytest.approx(0.31)

    suggestion = suggestion_for(submission, evidence)
    assert suggestion.reason == "SPAM"
    assert suggestion.reason_confidence == pytest.approx(0.31)
    assert suggestion.reason_source == "model"


@pytest.mark.django_db
def test_a_stored_unexplainable_rejection_is_not_shown_as_a_suggestion(submitter):
    """Rows scored before the model stopped proposing a bare OTHER are still in the table, and
    a moderator pressing the button on one gets a 422 rather than a decision."""
    submission = DomainSubmission.objects.create(name="stale.example", submitted_by=submitter)
    evidence = ready_evidence("stale.example", suggested_action="REJECT",
                              suggested_reason="OTHER", confidence=0.93,
                              reason_source="model")

    suggestion = suggestion_for(submission, evidence)

    assert (suggestion.action, suggestion.reason, suggestion.reason_detail) == ("UNSURE", "", "")


@pytest.mark.django_db
def test_a_rule_scored_rejection_keeps_its_reason_and_carries_the_detail(submitter):
    a_404 = rules.EvidenceItem(
        "http_status", rules.REJECT, "Homepage returns HTTP 404", implies_action="REJECT",
        implies_reason="OTHER", implies_confidence=0.9).to_dict()
    submission = DomainSubmission.objects.create(name="dead.example", submitted_by=submitter)
    evidence = ready_evidence("dead.example", suggested_action="REJECT",
                              suggested_reason="OTHER", confidence=0.9,
                              reason_source="rule", evidence=[a_404])

    suggestion = suggestion_for(submission, evidence)

    assert (suggestion.action, suggestion.reason) == ("REJECT", "OTHER")
    assert suggestion.reason_detail == "Homepage returns HTTP 404"


@pytest.mark.django_db
def test_a_previous_rejection_of_the_same_domain_says_so_as_the_detail(submitter, established):
    """The prior-decision check implies OTHER too, and its label is the sentence."""
    DomainSubmission.objects.create(name="again.example", submitted_by=established,
                                    status="REJECTED")
    submission = DomainSubmission.objects.create(name="again.example", submitted_by=submitter)
    evidence = ready_evidence("again.example")

    suggestion = suggestion_for(submission, evidence)

    assert (suggestion.action, suggestion.reason) == ("REJECT", "OTHER")
    assert suggestion.reason_detail == "This domain has already been rejected before"


@pytest.mark.django_db
def test_a_rule_scored_other_with_no_check_left_behind_it_is_not_suggested(submitter):
    """reason_source records that a check decided the reason when the row was written, not
    that one still does. A check that stops being decisive - or stops implying OTHER - leaves
    stored rows claiming "rule" with nothing left to explain them, and a rejection whose
    detail cannot be written is one the API would refuse. So it goes back as UNSURE, and the
    detail is read off the evidence rather than assumed from the column."""
    demoted = rules.EvidenceItem(
        "http_status", rules.REJECT, "Homepage returns HTTP 404").to_dict()
    submission = DomainSubmission.objects.create(name="stale.example", submitted_by=submitter)
    evidence = ready_evidence("stale.example", suggested_action="REJECT",
                              suggested_reason="OTHER", confidence=0.9,
                              reason_source="rule", evidence=[demoted])

    suggestion = suggestion_for(submission, evidence)

    assert (suggestion.action, suggestion.reason, suggestion.reason_detail) == ("UNSURE", "", "")


# --------------------------------------------------------------------- queue parity

@pytest.mark.django_db
def test_queue_display_matches_suggestion_for(submitter, established):
    """The queue filters and orders in SQL; the rows are rendered by suggestion_for. Where the
    two disagree, a filter hides rows that are on screen and the ordering sorts on a number
    nobody is shown - so every branch of the adjustment is checked against the other."""
    DomainSubmission.objects.create(name="approved-before.example",
                                    submitted_by=established, status="APPROVED")
    DomainSubmission.objects.create(name="rejected-before.example",
                                    submitted_by=established, status="REJECTED")
    DomainSubmission.objects.create(name="dead-and-approved-before.example",
                                    submitted_by=established, status="APPROVED")

    a_404 = rules.EvidenceItem(
        "http_status", rules.REJECT, "Homepage returns HTTP 404", implies_action="REJECT",
        implies_reason="OTHER", implies_confidence=0.9).to_dict()

    cases = [
        # (domain, submitter, evidence overrides) - one per branch of the adjustment.
        ("first-timer-approve.example", submitter, {}),
        ("known-approve.example", established, {}),
        ("reject.example", submitter, {"suggested_action": "REJECT",
                                       "suggested_reason": "SPAM", "confidence": 0.91}),
        ("unsure.example", submitter, {"suggested_action": "UNSURE", "confidence": 0.1}),
        # A reason the model cannot explain, so not shown as a suggestion at all.
        ("unexplained.example", submitter, {"suggested_action": "REJECT",
                                            "suggested_reason": "OTHER", "confidence": 0.93}),
        # The same, from the other side: a row that says a check decided the reason, with
        # nothing implying OTHER left in its evidence to write the detail from.
        ("stale-rule.example", submitter,
         {"suggested_action": "REJECT", "suggested_reason": "OTHER", "confidence": 0.93,
          "reason_source": "rule",
          "evidence": [rules.EvidenceItem(
              "http_status", rules.REJECT, "Homepage returns HTTP 404").to_dict()]}),
        ("approved-before.example", submitter, {"suggested_action": "REJECT",
                                                "suggested_reason": "SPAM"}),
        ("rejected-before.example", submitter, {}),
        # A check that already decided it, at least as strongly as the prior decision does.
        ("dead-and-approved-before.example", submitter,
         {"suggested_action": "REJECT", "suggested_reason": "OTHER", "confidence": 0.9,
          "reason_source": "rule", "evidence": [a_404]}),
        ("still-crawling.example", submitter, None),
    ]
    for domain, user, overrides in cases:
        DomainSubmission.objects.create(name=domain, submitted_by=user)
        if overrides is None:
            DomainEvidence.objects.create(domain=domain, state=DomainEvidence.State.PENDING)
        else:
            ready_evidence(domain, **overrides)

    for submission in DomainSubmission.objects.filter(status="PENDING"):
        evidence = DomainEvidence.objects.filter(domain=submission.name).first()
        shown = suggestion_for(submission, evidence)
        row = annotate_queue(DomainSubmission.objects.filter(pk=submission.pk)).get()

        if shown is None:
            assert row.displayed_action is None, submission.name
            assert row.displayed_priority is None, submission.name
            continue
        assert row.displayed_action == shown.action, submission.name
        assert row.displayed_reason == shown.reason, submission.name
        assert row.displayed_confidence == pytest.approx(shown.confidence), submission.name
        assert row.displayed_priority == pytest.approx(shown.review_priority), submission.name


@pytest.mark.django_db
def test_queue_filters_find_a_withheld_approval_where_it_is_shown(client, moderator, submitter):
    """A moderator filtering for UNSURE is asking for the rows only a human can settle, which
    is exactly what a withheld approval is."""
    DomainSubmission.objects.create(name="withheld.example", submitted_by=submitter)
    ready_evidence("withheld.example", suggested_action="APPROVE", confidence=0.9)

    response = client.get(f"{QUEUE_URL}?suggested_action=UNSURE",
                          headers={"Authorization": token(moderator)})
    items = response.json()["items"]

    assert [item["name"] for item in items] == ["withheld.example"]
    assert items[0]["suggestion"]["action"] == "UNSURE"


@pytest.mark.django_db
def test_a_withheld_approval_outranks_a_confident_one(client, moderator, submitter,
                                                      established):
    """It is the 44%-wrong slice: it needs a human more than an approval we trust does."""
    DomainSubmission.objects.create(name="withheld.example", submitted_by=submitter)
    ready_evidence("withheld.example", suggested_action="APPROVE", confidence=0.95)
    DomainSubmission.objects.create(name="trusted.example", submitted_by=established)
    ready_evidence("trusted.example", suggested_action="APPROVE", confidence=0.95)

    response = client.get(QUEUE_URL, headers={"Authorization": token(moderator)})
    assert [item["name"] for item in response.json()["items"]] == [
        "withheld.example", "trusted.example"]


# --------------------------------------------------------------------- the artifact

class _StubModel:
    """A stand-in for ModerationModel. joblib only needs it to pickle and carry a version."""

    def __init__(self, version, score=0.5):
        self.version = version
        self.score = score

    def predict(self, examples):
        return [(self.score, "SPAM", self.score) for _ in examples]


class _StaleFeatureSetModel:
    """An artifact whose pickled featuriser no longer matches the code that loaded it."""
    version = "stale"

    def predict(self, examples):
        raise ValueError("X has 8 features, but LogisticRegression is expecting 9 features")


def artifact_bytes(model) -> bytes:
    buffer = io.BytesIO()
    joblib.dump(model, buffer)
    return buffer.getvalue()


@pytest.mark.django_db
def test_a_published_model_outlives_the_process_that_trained_it():
    """It used to be written into the source tree, so a deploy reverted every retrain and each
    worker replica kept its own copy."""
    publish(_StubModel("domain-mod-2026-09-01"), {"cold_start": {"pr_auc": 0.81}})

    reset_model_cache()      # a different worker, loading for the first time
    assert get_model().version == "domain-mod-2026-09-01"
    assert load_metrics()["cold_start"]["pr_auc"] == 0.81


@pytest.mark.django_db
def test_a_worker_picks_up_a_retrain_it_did_not_run(monkeypatch):
    """The retrain runs in one process and every process serves suggestions."""
    publish(_StubModel("domain-mod-2026-09-01"), {})
    assert get_model().version == "domain-mod-2026-09-01"

    monkeypatch.setattr(model_module, "MODEL_REFRESH_SECONDS", 0)
    ModerationModelArtifact.objects.create(
        version="domain-mod-2026-10-01", metrics={},
        model=artifact_bytes(_StubModel("domain-mod-2026-10-01")))

    assert get_model().version == "domain-mod-2026-10-01"


@pytest.mark.django_db
def test_a_second_retrain_on_the_same_day_still_reaches_the_other_workers(monkeypatch):
    """Versions are dated, so a same-day republish keeps the version and replaces the bytes.
    A worker comparing versions alone would serve the superseded model until it restarted."""
    publish(_StubModel("domain-mod-2026-09-01"), {"cold_start": {"pr_auc": 0.5}})
    assert get_model().predict([None]) == [(0.5, "SPAM", 0.5)]

    monkeypatch.setattr(model_module, "MODEL_REFRESH_SECONDS", 0)
    # publish() drops the cache of the process that ran it, and this is any other worker.
    monkeypatch.setattr(model_module, "reset_model_cache", lambda: None)
    publish(_StubModel("domain-mod-2026-09-01", score=0.9), {"cold_start": {"pr_auc": 0.8}})

    assert get_model().predict([None]) == [(0.9, "SPAM", 0.9)]
    assert load_metrics()["cold_start"]["pr_auc"] == 0.8


@pytest.mark.django_db
def test_the_bundled_artifact_is_the_warm_start_until_a_retrain_publishes(tmp_path, settings):
    """A deploy against a database with no artifact in it still suggests something."""
    joblib.dump(_StubModel("bundled"), tmp_path / "model.joblib")
    (tmp_path / "metrics.json").write_text(json.dumps({"cold_start": {"pr_auc": 0.7}}))
    settings.DOMAIN_MODERATION_MODEL_DIR = str(tmp_path)

    assert get_model().version == "bundled"
    assert load_metrics()["cold_start"]["pr_auc"] == 0.7

    publish(_StubModel("domain-mod-2026-09-01"), {"cold_start": {"pr_auc": 0.81}})
    # Once something is published the gate compares against that, not against the shipped
    # numbers, which describe a model nobody is serving any more.
    assert load_metrics()["cold_start"]["pr_auc"] == 0.81


@pytest.mark.django_db
def test_an_unloadable_artifact_falls_back_rather_than_failing(tmp_path, settings):
    """A scikit-learn upgrade can make an old pickle unloadable, and the enrichment task must
    not go down with it."""
    joblib.dump(_StubModel("bundled"), tmp_path / "model.joblib")
    settings.DOMAIN_MODERATION_MODEL_DIR = str(tmp_path)
    ModerationModelArtifact.objects.create(
        version="corrupt", model=b"not a pickle", metrics={})

    assert get_model().version == "bundled"


@pytest.mark.django_db
def test_an_artifact_from_a_different_feature_set_degrades_to_the_rules(tmp_path, settings):
    """A featuriser is pickled whole, so adding a feature leaves every stored artifact building
    a matrix its heads were not fitted on. That raises at predict time, inside the enrichment
    task, for every domain - so loading probes the model once and drops it if it cannot answer,
    which is the same degradation an unloadable pickle already gets."""
    joblib.dump(_StaleFeatureSetModel(), tmp_path / "model.joblib")
    settings.DOMAIN_MODERATION_MODEL_DIR = str(tmp_path)

    assert get_model() is None
    suggestion = suggest("a.com", [], [])
    assert (suggestion.action, suggestion.model_version) == ("UNSURE", "")


@pytest.mark.django_db
def test_a_model_newer_than_the_code_degrades_instead_of_ending_the_run(tmp_path, settings):
    """is_compatible() protects the running code from an artifact older than it. Nothing can
    protect it from a *newer* one: a featuriser is pickled whole, so a model trained by code
    with one more feature produces a matrix the workers cannot use and they cannot know that
    until they try. A retrain from a freshly built image did exactly that in August 2026 and
    failed all 2,409 rows of a queue rescore, retrying forever on the same exception."""
    joblib.dump(_StaleFeatureSetModel(), tmp_path / "model.joblib")
    settings.DOMAIN_MODERATION_MODEL_DIR = str(tmp_path)

    # The model is handed in directly, as a worker holding an already-loaded one would have it,
    # so the load-time probe is bypassed exactly the way it is in production.
    suggestion = suggest("a.com", [], [], model=_StaleFeatureSetModel())

    assert (suggestion.action, suggestion.confidence) == ("UNSURE", 0.0)
    assert suggestion.model_version == ""


@pytest.mark.django_db
def test_a_decisive_check_still_wins_over_a_model_that_cannot_score():
    """The deterministic checks do not need the model, so an unscorable one must not downgrade
    a decisive answer to UNSURE."""
    items = [rules.EvidenceItem(kind="http_error", direction="REJECT",
                                label="Homepage returns 404", implies_action="REJECT",
                                implies_reason="OTHER", implies_confidence=0.95)]

    suggestion = suggest("a.com", [], items, model=_StaleFeatureSetModel())

    assert suggestion.action == "REJECT"
    assert suggestion.reason_source == "rule"


@pytest.mark.django_db
def test_a_sibling_deployments_artifact_does_not_evict_a_working_model(monkeypatch):
    """api and beta share a Postgres instance, so they share this table. The newest row is
    routinely the other deployment's, trained by code with a different feature set - and
    swapping a working model for a fallback once a minute because a sibling retrained is worse
    than ignoring the row."""
    publish(_StubModel("domain-mod-2026-09-01"), {})
    assert get_model().version == "domain-mod-2026-09-01"

    monkeypatch.setattr(model_module, "MODEL_REFRESH_SECONDS", 0)
    ModerationModelArtifact.objects.create(
        version="from-the-other-app", metrics={},
        model=artifact_bytes(_StaleFeatureSetModel()))

    assert get_model().version == "domain-mod-2026-09-01"


@pytest.mark.django_db
def test_an_unusable_artifact_is_not_unpickled_on_every_refresh(monkeypatch):
    """The rejection is remembered by stamp, so a sibling app's model costs one load rather
    than one per minute for as long as it stays the newest row."""
    publish(_StubModel("domain-mod-2026-09-01"), {})
    get_model()

    monkeypatch.setattr(model_module, "MODEL_REFRESH_SECONDS", 0)
    ModerationModelArtifact.objects.create(
        version="from-the-other-app", metrics={},
        model=artifact_bytes(_StaleFeatureSetModel()))

    loads = []
    original = model_module._load
    monkeypatch.setattr(model_module, "_load",
                        lambda version: loads.append(version) or original(version))

    for _ in range(3):
        assert get_model().version == "domain-mod-2026-09-01"
    assert loads == ["from-the-other-app"]


# --------------------------------------------------------------------- request validation

@pytest.mark.django_db
def test_an_over_long_audit_field_is_a_validation_error_not_a_database_one(
        client, moderator, submitter):
    """Django does not enforce max_length on save(), so without the schema constraint this is
    a Postgres DataError and a 500 rather than a 400 naming the field."""
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        f"/api/v1/platform/domain-submissions/ids/{submission.id}",
        data={"status": "APPROVED", "rejection_reason": "", "rejection_detail": "",
              "suggestion_model_version": "v" * 100},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 422
    submission.refresh_from_db()
    assert submission.status == "PENDING"


@pytest.mark.django_db
def test_other_without_a_detail_is_refused(client, moderator, submitter):
    """The dialog labels the fourth reason "Other - needs detail", and the detail is the only
    place a rejected submitter finds out what was actually wrong."""
    DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "example.com", "status": "REJECTED",
                             "rejection_reason": "OTHER", "rejection_detail": "  "}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 422
    assert DomainSubmission.objects.get(name="example.com").status == "PENDING"


@pytest.mark.django_db
def test_a_rejection_reason_outside_the_four_choices_is_refused(client, moderator, submitter):
    """Django does not enforce choices on save(), so any string of twenty characters or fewer
    lands in the column and comes back out at a moderator as a reason nothing can render."""
    DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "example.com", "status": "REJECTED",
                             "rejection_reason": "BECAUSE"}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 422


@pytest.mark.django_db
def test_a_decision_carrying_the_suggestion_action_as_its_status_is_refused(
        client, moderator, submitter):
    """"APPROVE" is what the tool suggests; "APPROVED" is what a submission becomes. Django
    does not check choices on save(), so the near-miss used to be written to every submission
    of the domain, which then matched neither the pending queue nor the approved set."""
    DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "example.com", "status": "APPROVE",
                             "suggested_status": "APPROVE"}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 422
    assert DomainSubmission.objects.get(name="example.com").status == "PENDING"


@pytest.mark.django_db
def test_an_approval_cannot_carry_a_rejection_reason(client, moderator, submitter):
    DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "example.com", "status": "APPROVED",
                             "rejection_reason": "SPAM"}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 422


@pytest.mark.django_db
def test_other_with_a_detail_is_accepted(client, moderator, submitter):
    DomainSubmission.objects.create(name="example.com", submitted_by=submitter)

    response = client.post(
        DECISIONS_URL,
        data={"decisions": [{"domain": "example.com", "status": "REJECTED",
                             "rejection_reason": "OTHER",
                             "rejection_detail": "Mirrors content we already index."}]},
        content_type="application/json", headers={"Authorization": token(moderator)})

    assert response.status_code == 200
    assert DomainSubmission.objects.get(name="example.com").status == "REJECTED"
