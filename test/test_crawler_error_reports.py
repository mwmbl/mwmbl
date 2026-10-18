"""
Crawler errors reach us: the crawl loops catch and retry every exception, so without a
report a crawler failing on every pass looks the same as one that has stopped.

Covers the crawler's report_error and POST /api/v1/crawler/errors, which passes reports on
to Sentry.
"""

from unittest.mock import patch

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl import crawl
from mwmbl.models import ApiKey, generate_api_key

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


def post_report(raw_key):
    return Client().post("/api/v1/crawler/errors", content_type="application/json", data=REPORT, HTTP_X_API_KEY=raw_key)


@pytest.mark.django_db
def test_a_report_goes_to_sentry_as_an_exception_with_its_traceback():
    user, raw_key = make_api_key(ApiKey.Scope.CRAWL)

    with patch("mwmbl.crawler.app.sentry_sdk.capture_event") as capture_event:
        response = post_report(raw_key)

    assert response.status_code == 200
    event = capture_event.call_args.args[0]
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


def test_a_failed_report_does_not_raise(reporting):
    reporting.side_effect = requests.ConnectionError("server down")

    crawl.report_error("indexing", raise_and_catch(ValueError("bad page")))
