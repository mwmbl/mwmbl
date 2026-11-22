"""
Crawler errors reach us: the crawl loops catch and retry every exception, so without a
report a crawler failing on every pass looks the same as one that has stopped.

Covers POST /api/v1/crawler/errors, which passes reports on to Sentry.
"""

from unittest.mock import patch

import pytest
import sentry_sdk
from django.contrib.auth import get_user_model
from django.test import Client
from sentry_sdk.transport import Transport

from mwmbl import crawler_errors
from mwmbl.crawler.batch import MAX_ERROR_MESSAGE_LENGTH, MAX_ERROR_TAG_LENGTH
from mwmbl.models import ApiKey, CrawlerError, generate_api_key

User = get_user_model()

REPORT = {
    "context": "indexing",
    "exception_type": "requests.exceptions.HTTPError",
    "message": "502 Server Error: Bad Gateway",
    "frames": [
        {"filename": "mwmbl/crawl.py", "lineno": 400, "function": "run_indexing_continuously", "line": None},
        {
            "filename": "mwmbl/crawl.py",
            "lineno": 312,
            "function": "run_indexing",
            "line": "response.raise_for_status()",
        },
    ],
    "crawler_version": "1.2.0",
    "device_name": "crawler-3f7a1c9e",
}


def make_api_key(scope):
    user = User.objects.create_user(username=f"user-{scope}", email=f"{scope}@example.com", password="pass")
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Test key", scopes=[scope])
    return user, raw_key


def post_report(raw_key, report=REPORT):
    return Client().post("/api/v1/crawler/errors", content_type="application/json", data=report, HTTP_X_API_KEY=raw_key)


class CapturingTransport(Transport):
    def __init__(self, options):
        super().__init__(options)
        self.events = []

    def capture_envelope(self, envelope):
        self.events.extend(item.payload.json for item in envelope.items if item.type == "event")


@pytest.fixture
def sentry_events():
    """The events a real Sentry client sends, so the SDK's own defaults are part of the test."""
    with sentry_sdk.init(dsn="https://key@example.ingest.sentry.io/1", transport=CapturingTransport, release="server"):
        transport = sentry_sdk.get_client().transport
        yield transport.events


@pytest.mark.django_db
def test_a_report_goes_to_sentry_once_as_a_crawler_exception(sentry_events):
    user, raw_key = make_api_key(ApiKey.Scope.CRAWL)

    response = post_report(raw_key)
    sentry_sdk.flush()

    assert response.status_code == 200
    [event] = sentry_events
    exception = event["exception"]["values"][0]
    assert exception["type"] == "requests.exceptions.HTTPError"
    assert exception["value"] == "502 Server Error: Bad Gateway"
    assert [frame["function"] for frame in exception["stacktrace"]["frames"]] == [
        "run_indexing_continuously",
        "run_indexing",
    ]
    assert event["environment"] == "crawler"
    assert event["release"] == "mwmbl-crawler@1.2.0"
    assert event["server_name"] == "crawler-3f7a1c9e"
    assert event["user"] == {"id": str(user.id)}


@pytest.mark.django_db
def test_a_report_is_kept_for_its_user_with_the_innermost_frame_as_its_location():
    user, raw_key = make_api_key(ApiKey.Scope.CRAWL)

    post_report(raw_key)

    [error] = CrawlerError.objects.filter(user=user)
    assert error.device_name == "crawler-3f7a1c9e"
    assert error.exception_type == "requests.exceptions.HTTPError"
    assert error.message == "502 Server Error: Bad Gateway"
    assert error.location == "mwmbl/crawl.py:312 in run_indexing"
    assert error.crawler_version == "1.2.0"


@pytest.mark.django_db
def test_a_frame_without_a_line_number_has_a_location_without_one():
    user, raw_key = make_api_key(ApiKey.Scope.CRAWL)
    frame = {"filename": "mwmbl/crawl.py", "lineno": None, "function": "run_indexing", "line": None}

    post_report(raw_key, {**REPORT, "frames": [frame]})

    [error] = CrawlerError.objects.filter(user=user)
    assert error.location == "mwmbl/crawl.py in run_indexing"


@pytest.mark.django_db
def test_only_a_users_newest_errors_are_kept(monkeypatch):
    monkeypatch.setattr(crawler_errors, "MAX_ERRORS_PER_USER", 3)
    user, raw_key = make_api_key(ApiKey.Scope.CRAWL)
    other_user = User.objects.create_user(username="other", email="other@example.com", password="pass")
    CrawlerError.objects.create(
        user=other_user,
        device_name="d",
        context="batch",
        exception_type="E",
        message="m",
        location="l",
        crawler_version="1",
    )

    for number in range(5):
        post_report(raw_key, {**REPORT, "message": f"error {number}"})

    kept = CrawlerError.objects.filter(user=user).order_by("-id").values_list("message", flat=True)
    assert list(kept) == ["error 4", "error 3", "error 2"]
    assert CrawlerError.objects.filter(user=other_user).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"crawler_version": None},
        {"device_name": None},
        {"context": "anything"},
        {"frames": []},
        {"message": "x" * (MAX_ERROR_MESSAGE_LENGTH + 1)},
        {"device_name": "x" * (MAX_ERROR_TAG_LENGTH + 1)},
    ],
)
def test_a_report_sentry_could_misfile_or_drop_is_rejected(change):
    raw_key = make_api_key(ApiKey.Scope.CRAWL)[1]

    with patch("mwmbl.crawler.app.sentry_sdk.capture_event") as capture_event:
        response = post_report(raw_key, {**REPORT, **change})

    assert response.status_code == 422
    capture_event.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("scope", [ApiKey.Scope.SEARCH, None])
def test_a_report_needs_a_crawl_key(scope):
    raw_key = make_api_key(scope)[1] if scope else ""

    with patch("mwmbl.crawler.app.sentry_sdk.capture_event") as capture_event:
        response = post_report(raw_key)

    assert response.status_code == 401
    capture_event.assert_not_called()
