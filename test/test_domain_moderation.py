"""Tests for the domain moderation suggester and the moderator queue API.

The load-bearing property here is not that the model is accurate - that is measured offline by
the training gate - but that a moderator is never shown something misleading: a suggestion is
either backed by evidence or absent, a deterministic check always beats a probability, and the
queue never runs a model on the request path.
"""
from datetime import timedelta
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Permission
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import DomainEvidence, DomainSubmission, MwmblUser
from mwmbl.moderation import rules
from mwmbl.moderation.features import Featuriser, ModerationExample
from mwmbl.moderation.model import Suggestion, suggest
from mwmbl.moderation.suggest import refresh_suggestion, suggestion_for
from mwmbl.moderation.train import passes_gate
from mwmbl.moderation.training_data import TrainingRow, is_trainable_domain

QUEUE_URL = "/api/v1/platform/domain-submissions/queue"


@pytest.fixture
def submitter(db):
    return MwmblUser.objects.create_user(
        username="submitter", email="submitter@example.com", password="password")


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
        "review_priority": 0.2,
        "evidence": [],
        "model_version": "test-model",
    }
    return DomainEvidence.objects.create(domain=domain, **(defaults | overrides))


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
def test_queue_orders_rows_that_need_a_human_first(client, moderator, submitter):
    for name, action, priority in [("approve-me.com", "APPROVE", 0.1),
                                   ("reject-me.com", "REJECT", 1.9),
                                   ("unsure.com", "UNSURE", 1.0)]:
        DomainSubmission.objects.create(name=name, submitted_by=submitter)
        ready_evidence(name, suggested_action=action, review_priority=priority)

    response = client.get(QUEUE_URL, headers={"Authorization": token(moderator)})
    assert [item["name"] for item in response.json()["items"]] == [
        "reject-me.com", "unsure.com", "approve-me.com"]


@pytest.mark.django_db
def test_queue_filters_on_the_stored_suggestion(client, moderator, submitter):
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
            {"submission_id": first.id, "status": "APPROVED"},
            {"submission_id": second.id, "status": "REJECTED", "rejection_reason": "SPAM"},
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
def test_queue_cost_does_not_grow_with_the_backlog(client, moderator, submitter,
                                                   django_assert_max_num_queries):
    """Ordering, filtering and slicing must happen in SQL.

    The backlog is ~4,000 pending submissions. An implementation that builds the list in
    Python and sorts it there pays for all of them on every request, and issues per-row
    queries for the submitter and prior-decision checks on top. Both are invisible on a small
    test fixture, so this pins the query count instead: it must be flat in the number of rows.
    """
    for index in range(30):
        name = f"site{index}.example"
        DomainSubmission.objects.create(name=name, submitted_by=submitter)
        ready_evidence(name, review_priority=index / 30)

    # Auth, permissions, count, page, evidence, submitter counts, prior-decision counts.
    with django_assert_max_num_queries(12):
        response = client.get(f"{QUEUE_URL}?limit=5",
                              headers={"Authorization": token(moderator)})

    body = response.json()
    assert body["count"] == 30
    assert len(body["items"]) == 5
    # ...and the page is the top of the global ordering, not the top of an arbitrary slice.
    assert body["items"][0]["name"] == "site29.example"


@pytest.mark.django_db
def test_queue_paginates_over_the_whole_ordering(client, moderator, submitter):
    for index in range(10):
        name = f"site{index}.example"
        DomainSubmission.objects.create(name=name, submitted_by=submitter)
        ready_evidence(name, review_priority=index / 10)

    def page(offset):
        response = client.get(f"{QUEUE_URL}?limit=4&offset={offset}",
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

def metrics(pr_auc, low, high):
    return {"cold_start": {"pr_auc": pr_auc, "pr_auc_ci": [low, high]}}


def test_gate_publishes_when_there_is_no_incumbent():
    allowed, _ = passes_gate(metrics(0.78, 0.69, 0.86), {})
    assert allowed


def test_gate_blocks_a_model_below_the_incumbents_lower_bound():
    allowed, explanation = passes_gate(metrics(0.60, 0.51, 0.69),
                                       metrics(0.78, 0.69, 0.86))
    assert not allowed
    assert "0.690" in explanation


def test_gate_tolerates_movement_inside_the_interval():
    """The measured augmentation experiment moved the point estimate by 0.04 while the
    intervals spanned 0.17. A gate on point estimates would treat that noise as signal in
    both directions - blocking good models and shipping bad ones."""
    allowed, _ = passes_gate(metrics(0.74, 0.65, 0.83), metrics(0.78, 0.69, 0.86))
    assert allowed


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
