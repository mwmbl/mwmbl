"""Tests for the search traffic client classifier.

The important one is test_every_upstream_instance_is_a_declared_bot: it drives off the
vendored file itself, so a refresh that broke the matcher fails here rather than quietly
reclassifying every crawler as a browser.
"""

import json
import time

import pytest
from django.test import RequestFactory

from mwmbl.traffic import (
    CRAWLER_USER_AGENTS_PATH,
    MAX_USER_AGENT_LENGTH,
    ClientClass,
    classify_client,
    is_declared_bot,
)

BROWSER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
    "Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
    "Mobile Safari/537.36",
]


@pytest.fixture
def get_request():
    factory = RequestFactory()

    def make(**headers):
        return factory.get("/", **headers)

    return make


def test_every_upstream_instance_is_a_declared_bot():
    with open(CRAWLER_USER_AGENTS_PATH) as patterns_file:
        instances = json.load(patterns_file)["instances"]

    assert len(instances) > 2000
    missed = [instance for instance in instances if not is_declared_bot(instance)]
    assert missed == []


@pytest.mark.parametrize("user_agent", BROWSER_USER_AGENTS)
def test_real_browsers_are_not_declared_bots(user_agent):
    assert not is_declared_bot(user_agent)


@pytest.mark.parametrize("user_agent", BROWSER_USER_AGENTS)
def test_a_browser_with_a_language_is_a_browser(get_request, user_agent):
    request = get_request(HTTP_USER_AGENT=user_agent, HTTP_ACCEPT_LANGUAGE="en-GB,en;q=0.9")
    assert classify_client(request) == ClientClass.BROWSER


@pytest.mark.parametrize(
    "user_agent",
    [
        "Googlebot/2.1 (+http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
        "curl/8.5.0",
        "python-requests/2.31.0",
        "Wget/1.21.4",
    ],
)
def test_declared_bots_beat_every_other_signal(get_request, user_agent):
    # No Accept-Language either, so this also pins the precedence: a bot that fails the
    # plausibility checks is still reported as a bot, not as an implausible client.
    request = get_request(HTTP_USER_AGENT=user_agent)
    assert classify_client(request) == ClientClass.DECLARED_BOT


def test_no_user_agent_is_implausible(get_request):
    request = get_request(HTTP_ACCEPT_LANGUAGE="en-GB")
    assert classify_client(request) == ClientClass.IMPLAUSIBLE


def test_bare_mozilla_is_implausible(get_request):
    request = get_request(HTTP_USER_AGENT="Mozilla/5.0", HTTP_ACCEPT_LANGUAGE="en-GB")
    assert classify_client(request) == ClientClass.IMPLAUSIBLE


def test_a_browser_without_a_language_is_implausible(get_request):
    request = get_request(HTTP_USER_AGENT=BROWSER_USER_AGENTS[0])
    assert classify_client(request) == ClientClass.IMPLAUSIBLE


def test_the_front_end_as_it_calls_us_today_is_implausible(get_request):
    """SSR uses a bare fetch() and forwards nothing - see mwmbl/front-end#55."""
    request = get_request(HTTP_USER_AGENT="node")
    assert classify_client(request) == ClientClass.IMPLAUSIBLE


def test_a_long_user_agent_is_truncated_before_matching():
    padding = "x" * (MAX_USER_AGENT_LENGTH * 20)
    assert not is_declared_bot(padding + "Googlebot/")
    assert is_declared_bot("Googlebot/" + padding)


def test_classifying_a_browser_is_fast():
    """The regression test for the naive alternation, which took 4.5 ms per browser hit.

    Uncached, because the cache is what makes the steady state free and would hide it.
    """
    is_declared_bot.cache_clear()
    start = time.perf_counter()
    is_declared_bot(BROWSER_USER_AGENTS[0])
    assert time.perf_counter() - start < 0.001
