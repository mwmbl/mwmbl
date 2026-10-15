"""
Behaviour of the curation and domain pages in mwmbl.views: the state machine behind
approving a result, the add-URL flow, reverting and flagging curations, and the
domain submission pages.
"""

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from mwmbl.models import Curation, DomainSubmission, FlagCuration, MwmblUser
from mwmbl.tinysearchengine.indexer import Document, DocumentState
from mwmbl.views import _get_document_state, _insert_document, switch_state


@pytest.fixture
def curator(db):
    return MwmblUser.objects.create_user(username="curator", email="curator@example.com", password="pw")


@pytest.fixture
def moderator(db):
    user = MwmblUser.objects.create_user(username="moderator", email="moderator@example.com", password="pw")
    permissions = Permission.objects.filter(
        codename__in=["change_flag_status", "change_domain_submission_status"],
    )
    user.user_permissions.set(permissions)
    return user


@pytest.fixture
def curation(curator):
    return Curation.objects.create(
        user=curator,
        timestamp=timezone.now(),
        query="curation views",
        original_index_results=[],
        original_results=[
            {"title": "Orig", "url": "https://example.com/orig", "extract": "e", "score": 1.0, "term": "curation view"}
        ],
        new_results=[],
        num_changes=1,
    )


def _result(url: str, state: DocumentState | None = None) -> Document:
    return Document(title=url, url=url, extract="extract", score=1.0, term="t", state=state)


@pytest.mark.parametrize(
    "state, expected",
    [
        (None, DocumentState.ORGANIC_APPROVED),
        (DocumentState.FROM_GOOGLE, DocumentState.FROM_GOOGLE_APPROVED),
        (DocumentState.FROM_USER, DocumentState.FROM_USER_APPROVED),
        (DocumentState.FROM_WIKI, DocumentState.FROM_WIKI_APPROVED),
        (DocumentState.FROM_GOOGLE_APPROVED, DocumentState.FROM_GOOGLE),
        (DocumentState.FROM_USER_APPROVED, DocumentState.FROM_USER),
        (DocumentState.FROM_WIKI_APPROVED, DocumentState.FROM_WIKI),
        (DocumentState.ORGANIC_APPROVED, None),
    ],
)
def test_switch_state_toggles_approval(state, expected):
    assert switch_state(state) == expected


def test_switch_state_rejects_unknown_state():
    with pytest.raises(ValueError):
        switch_state("not a state")


@pytest.mark.parametrize(
    "validated, source, expected",
    [
        (True, "user", DocumentState.FROM_USER_APPROVED),
        (True, "Google", DocumentState.FROM_GOOGLE_APPROVED),
        (True, "organic", DocumentState.ORGANIC_APPROVED),
        (False, "USER", DocumentState.FROM_USER),
        (False, "google", DocumentState.FROM_GOOGLE),
        (False, "organic", None),
    ],
)
def test_document_state_from_validation_and_source(validated, source, expected):
    assert _get_document_state(validated, source) == expected


def test_insert_document_goes_above_unapproved_results():
    approved = _result("https://example.com/approved", DocumentState.ORGANIC_APPROVED)
    documents = {
        "https://example.com/a": _result("https://example.com/a", DocumentState.ORGANIC_APPROVED),
        "https://example.com/b": _result("https://example.com/b"),
    }

    reranked = _insert_document(documents, approved)

    assert [d.url for d in reranked] == [
        "https://example.com/a",
        "https://example.com/approved",
        "https://example.com/b",
    ]


def test_insert_document_goes_last_when_everything_is_approved():
    approved = _result("https://example.com/approved", DocumentState.ORGANIC_APPROVED)
    documents = {"https://example.com/a": _result("https://example.com/a", DocumentState.ORGANIC_APPROVED)}

    reranked = _insert_document(documents, approved)

    assert [d.url for d in reranked] == ["https://example.com/a", "https://example.com/approved"]


def test_insert_document_replaces_an_existing_copy_of_the_url():
    approved = _result("https://example.com/a", DocumentState.ORGANIC_APPROVED)
    documents = {"https://example.com/a": _result("https://example.com/a")}

    reranked = _insert_document(documents, approved)

    assert reranked == [approved]


class FakeResponse:
    encoding = "utf-8"
    text = (
        "<html><head><title>Fetched page</title></head><body><p>"
        + "This is a long paragraph of good text that the extractor should keep for the extract. " * 12
        + "</p></body></html>"
    )


@pytest.mark.django_db
def test_add_url_fetches_the_page_and_records_a_curation(client, curator, monkeypatch):
    monkeypatch.setattr("mwmbl.views.validate_url", lambda url: None)
    monkeypatch.setattr("mwmbl.views.requests.get", lambda *args, **kwargs: FakeResponse())
    client.force_login(curator)

    response = client.post(
        reverse("add_url"),
        {
            "new_url": "https://example.com/fetched",
            "query": "fetched page",
            "url": ["https://example.com/other"],
            "title": ["Other"],
            "extract": ["Other extract"],
            "state": [""],
            "score": ["1.0"],
        },
    )

    assert response.status_code == 200
    curation = Curation.objects.get()
    new_urls = [result["url"] for result in curation.new_results]
    assert "https://example.com/fetched" in new_urls


@pytest.mark.django_db
def test_add_url_reports_fetch_failures(client, curator, monkeypatch):
    from requests import RequestException

    def fail(*args, **kwargs):
        raise RequestException("connection refused")

    monkeypatch.setattr("mwmbl.views.validate_url", lambda url: None)
    monkeypatch.setattr("mwmbl.views.requests.get", fail)
    client.force_login(curator)

    response = client.post(reverse("add_url"), {"new_url": "https://example.com/down", "query": "down"})

    assert response.status_code == 400
    assert "connection refused" in response.content.decode()


def _approve(client, curation_id: int | None = None):
    data = {
        "query": "curation views",
        "approve_url": "https://example.com/orig",
        "url": ["https://example.com/orig"],
        "title": ["Orig"],
        "extract": ["e"],
        "state": [""],
        "score": ["1.0"],
    }
    if curation_id is not None:
        data["curation_id"] = str(curation_id)
    return client.post(reverse("approve"), data)


@pytest.mark.django_db
def test_approving_again_updates_the_same_curation(client, curator):
    client.force_login(curator)
    _approve(client)
    first = Curation.objects.get()

    response = _approve(client, curation_id=first.id)

    assert response.status_code == 200
    first.refresh_from_db()
    assert Curation.objects.count() == 1
    assert first.num_changes == 2


@pytest.mark.django_db
def test_reverting_a_curation_deletes_it_and_shows_the_original_results(client, curator, curation):
    client.force_login(curator)

    response = client.post(reverse("revert_current"), {"curation_id": curation.id})

    assert response.status_code == 200
    assert not Curation.objects.filter(id=curation.id).exists()
    assert "Orig" in response.content.decode()


@pytest.mark.django_db
def test_curation_pages_render(client, curator, curation):
    FlagCuration.objects.create(user=curator, timestamp=timezone.now(), curation=curation, flag="OTHER")

    list_response = client.get(reverse("curations"))
    detail_response = client.get(reverse("curation", args=[curation.id]))

    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert [flag.flag for flag in detail_response.context["flags"]] == ["OTHER"]


@pytest.mark.django_db
def test_flagging_requires_login(client, curation):
    response = client.get(reverse("flag_curation", args=[curation.id]))

    assert response.status_code == 302
    assert "/login" in response["Location"]


@pytest.mark.django_db
def test_flag_form_is_shown_and_rejects_an_invalid_flag(client, curator, curation):
    client.force_login(curator)
    url = reverse("flag_curation", args=[curation.id])

    form_response = client.get(url)
    invalid_response = client.post(url, {"flag": "NOT_A_FLAG"})

    assert form_response.status_code == 200
    assert invalid_response.status_code == 200
    assert not FlagCuration.objects.exists()


@pytest.mark.django_db
def test_pending_flags_are_listed_for_logged_in_users(client, curator, curation):
    FlagCuration.objects.create(user=curator, timestamp=timezone.now(), curation=curation, flag="OTHER")
    FlagCuration.objects.create(
        user=curator, timestamp=timezone.now(), curation=curation, flag="OTHER", status="REJECTED"
    )
    client.force_login(curator)

    response = client.get(reverse("flag_curation_list"))

    assert response.status_code == 200
    assert [flag.status for flag in response.context["object_list"]] == ["PENDING"]


@pytest.mark.django_db
def test_flag_update_rejects_an_unknown_status(client, moderator, curation):
    flag = FlagCuration.objects.create(user=moderator, timestamp=timezone.now(), curation=curation, flag="OTHER")
    client.force_login(moderator)

    response = client.post(reverse("flag_curation_update", args=[flag.id]), {"status": "BOGUS"})

    assert response.status_code == 400


@pytest.mark.django_db
def test_flag_update_requires_the_permission(client, curator, curation):
    flag = FlagCuration.objects.create(user=curator, timestamp=timezone.now(), curation=curation, flag="OTHER")
    client.force_login(curator)

    response = client.post(reverse("flag_curation_update", args=[flag.id]), {"status": "REJECTED"})

    assert response.status_code == 302
    flag.refresh_from_db()
    assert flag.status == "PENDING"


@pytest.mark.django_db
def test_rejecting_a_flag_leaves_the_curation_alone(client, moderator, curation):
    flag = FlagCuration.objects.create(user=moderator, timestamp=timezone.now(), curation=curation, flag="OTHER")
    client.force_login(moderator)

    response = client.post(reverse("flag_curation_update", args=[flag.id]), {"status": "REJECTED"})

    assert response.status_code == 200
    flag.refresh_from_db()
    assert flag.status == "REJECTED"
    assert Curation.objects.filter(id=curation.id).exists()


@pytest.mark.django_db
def test_accepting_a_flag_reverts_the_curation(client, moderator, curation):
    flag = FlagCuration.objects.create(user=moderator, timestamp=timezone.now(), curation=curation, flag="OTHER")
    client.force_login(moderator)

    response = client.post(reverse("flag_curation_update", args=[flag.id]), {"status": "ACCEPTED"})

    assert response.status_code == 200
    flag.refresh_from_db()
    assert flag.status == "ACCEPTED"


@pytest.mark.django_db
def test_submit_domain_form_and_submission(client, curator):
    client.force_login(curator)
    url = reverse("submit_domain")

    form_response = client.get(url)
    post_response = client.post(url, {"name": "https://www.example.org/some/page"})

    assert form_response.status_code == 200
    assert post_response.status_code == 302
    submission = DomainSubmission.objects.get()
    assert submission.name == "www.example.org"
    assert submission.submitted_by == curator


@pytest.mark.django_db
def test_invalid_domain_is_not_saved(client, curator):
    client.force_login(curator)

    response = client.post(reverse("submit_domain"), {"name": "not a domain"})

    assert response.status_code == 200
    assert not DomainSubmission.objects.exists()


@pytest.mark.django_db
def test_moderator_can_decide_a_submission_on_the_domain_page(client, moderator, curator):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=curator)
    client.force_login(moderator)

    response = client.post(
        reverse("domain", args=["example.org"]),
        {"id": submission.id, "status": "REJECTED", "rejection_reason": "OTHER", "rejection_detail": "spam"},
    )

    assert response.status_code == 200
    submission.refresh_from_db()
    assert submission.status == "REJECTED"


@pytest.mark.django_db
def test_domain_page_hides_approval_forms_from_ordinary_users(client, curator):
    DomainSubmission.objects.create(name="example.org", submitted_by=curator)
    client.force_login(curator)

    response = client.get(reverse("domain", args=["example.org"]))

    assert response.status_code == 200
    assert response.context["forms"] == []


@pytest.mark.django_db
def test_domain_page_shows_pending_approval_forms_to_moderators(client, moderator, curator):
    DomainSubmission.objects.create(name="example.org", submitted_by=curator)
    client.force_login(moderator)

    response = client.get(reverse("domain", args=["example.org"]))

    assert len(response.context["forms"]) == 1
