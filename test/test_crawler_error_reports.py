"""
Crawler errors reach us: the crawl loops catch and retry every exception, so without a
report a crawler failing on every pass looks the same as one that has stopped.

Covers the crawler's report_error and POST /api/v1/crawler/errors, which passes reports on
to Sentry.
"""

from unittest.mock import patch

import pytest
import requests
import sentry_sdk
from django.contrib.auth import get_user_model
from django.test import Client
from sentry_sdk.transport import Transport

from mwmbl import crawl, crawler_errors
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


def raise_and_catch(error):
    try:
        raise error
    except Exception as caught:
        return caught


@pytest.fixture
def reporting(monkeypatch):
    monkeypatch.setattr(crawl, "_error_last_reported_at", {})
    monkeypatch.setattr(crawl, "CRAWL_REPORT_ERRORS", True)
    monkeypatch.setattr(crawl, "CRAWL_SUBMIT_MODE", crawl.SUBMIT_MODE_DRY_RUN)
    monkeypatch.setattr(crawl, "get_device_name", lambda data_path: "crawler-test")
    with patch("mwmbl.crawl.requests.post") as post:
        yield post


def test_report_error_posts_the_traceback(reporting):
    crawl.report_error("indexing", raise_and_catch(ValueError("bad page")))

    report = reporting.call_args.kwargs["json"]
    assert reporting.call_args.args[0] == f"{crawl.REMOTE_SERVER}/api/v1/crawler/errors"
    assert report["exception_type"] == "builtins.ValueError"
    assert report["message"] == "bad page"
    assert report["frames"][-1]["function"] == "raise_and_catch"
    assert report["device_name"] == "crawler-test"


def test_the_same_error_is_reported_once_an_hour_not_every_retry(reporting):
    def fail():
        return raise_and_catch(ValueError("same place"))

    for _ in range(3):
        crawl.report_error("indexing", fail())
    crawl.report_error("batch", fail())

    assert reporting.call_count == 2


@pytest.mark.parametrize(
    "setting, value", [("CRAWL_REPORT_ERRORS", False), ("CRAWL_SUBMIT_MODE", crawl.SUBMIT_MODE_OFF)]
)
def test_nothing_is_reported_when_turned_off(reporting, monkeypatch, setting, value):
    monkeypatch.setattr(crawl, setting, value)

    crawl.report_error("indexing", raise_and_catch(ValueError("bad page")))

    reporting.assert_not_called()


def test_the_report_does_not_name_the_volunteers_home_directory(reporting, monkeypatch, tmp_path):
    monkeypatch.setattr(crawl.Path, "home", lambda: tmp_path)
    error = raise_and_catch(FileNotFoundError(f"No such file: '{tmp_path}/.mwmbl/index'"))

    crawl.report_error("indexing", error)

    report = reporting.call_args.kwargs["json"]
    assert report["message"] == "No such file: '~/.mwmbl/index'"
    assert report["frames"][-1]["filename"] == "test_crawler_error_reports.py"


def test_a_frame_path_is_relative_to_its_python_path_entry(monkeypatch):
    monkeypatch.setattr(crawl.sys, "path", ["/home/jane/venv/lib/site-packages", "/home/jane"])

    assert crawl._module_path("/home/jane/venv/lib/site-packages/mwmbl/crawl.py") == "mwmbl/crawl.py"


def test_a_failed_report_is_logged_not_raised(reporting, caplog):
    reporting.side_effect = requests.ConnectionError("server down")

    crawl.report_error("indexing", raise_and_catch(ValueError("bad page")))

    assert "Failed to report error" in caplog.text
