"""Offline Jev composite ordering: production's questions and weights, with retries and a cache."""

from unittest.mock import MagicMock

import pytest

import mwmbl.rankeval.evaluation.jev_order as jev_order
from mwmbl.rankeval.evaluation.jev_order import order_query
from mwmbl.tinysearchengine.jev_rank import QUALITY_QUESTION, RELEVANCE_QUESTION

QUERY = "bankside hotel"
CANDIDATES = [
    {"url": "https://staan0.com/", "title": "Staan 0", "extract": "", "staan_position": 0},
    {"url": "https://staan1.com/", "title": "Staan 1", "extract": "", "staan_position": 1},
    {"url": "https://index.com/", "title": "Index", "extract": "", "staan_position": None},
]


def response(status: int, scores: tuple[tuple[float, float], ...] = ()) -> MagicMock:
    answers = {}
    for i, (relevance, quality) in enumerate(scores):
        answers[f"r{i}"] = {"score": relevance}
        answers[f"q{i}"] = {"score": quality}
    result = MagicMock(status_code=status)
    result.json.return_value = {"answers": answers}
    return result


@pytest.fixture
def jev(monkeypatch, settings):
    settings.JEV_API_KEY = "test-key"
    monkeypatch.setattr(jev_order.time, "sleep", lambda seconds: None)
    calls = []

    def configure(*responses):
        queued = list(responses)

        def fake_post(url, json, headers, timeout):
            calls.append(json)
            return queued.pop(0)

        monkeypatch.setattr(jev_order.requests, "post", fake_post)
        return calls

    return configure


def test_the_pool_is_sorted_by_the_production_composite(jev, tmp_path):
    # relevance + 0.5 x quality - 0.1 x Staan position - 0.5 if not from Staan:
    # staan0 3 + 1 - 0 = 4, staan1 1 + 1 - 0.1 = 1.9, index 3 + 1 - 1 - 0.5 = 2.5.
    calls = jev(response(200, ((3, 2), (1, 2), (3, 2))))

    ordered = order_query(QUERY, CANDIDATES, tmp_path)

    assert [result["url"] for result in ordered] == ["https://staan0.com/", "https://index.com/", "https://staan1.com/"]
    assert ordered[1]["relevance"] == 3 and ordered[1]["quality"] == 2
    questions = {question["instructions"]["question"] for question in calls[0]["questions"].values()}
    assert questions == {RELEVANCE_QUESTION, QUALITY_QUESTION}


def test_a_rate_limited_request_is_retried(jev, tmp_path):
    calls = jev(response(429), response(200, ((3, 2), (2, 2), (0, 0))))

    ordered = order_query(QUERY, CANDIDATES, tmp_path)

    assert len(calls) == 2
    assert ordered[0]["url"] == "https://staan0.com/"


def test_a_cached_response_is_not_requested_again(jev, tmp_path):
    calls = jev(response(200, ((3, 2), (2, 2), (0, 0))))

    first = order_query(QUERY, CANDIDATES, tmp_path)
    second = order_query(QUERY, CANDIDATES, tmp_path)

    assert len(calls) == 1
    assert first == second
