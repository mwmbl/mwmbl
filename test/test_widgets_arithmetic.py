import itertools
import time
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from ninja import NinjaAPI
from ninja.testing import TestClient

from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.widgets.arithmetic import calculate

API_NUMBERS = itertools.count()


@pytest.mark.parametrize(
    "query, expected",
    [
        ("164*8", "1312"),
        ("2+3*4", "14"),
        ("(2+3)*4", "20"),
        ("2^10", "1024"),
        ("2**3**2", "512"),
        ("-2^2", "-4"),
        ("2^-1", "0.5"),
        ("0.1+0.2", "0.3"),
        ("1/3", "0.333333333333"),
        ("10/4", "2.5"),
        ("7%3", "1"),
        ("-7%3", "2"),
        ("  3 - 5 ", "-2"),
        ("2^0.5", "1.41421356237"),
        ("10^20", "1e+20"),
        ("1/10^7", "1e-7"),
        ("10^15-1", "999999999999999"),
        ("10^100", "1e+100"),
    ],
)
def test_calculate(query, expected):
    assert calculate(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "164",
        "-5",
        "(5)",
        "weather in york",
        "1+1=2",
        "2 + abs(3)",
        "2e3+1",
        "__import__('os')+1",
        "2 (3)",
        "1/0",
        "5%0",
        "0^-1",
        "(-8)^0.5",
        "1.2.3+1",
        "2+",
        "((2+3)",
        "9^9^9",
        "9**9**9",
        "2^1001",
        "10^1000*10^1000",
        "10^100*10",
        "(10^50)^50",
        "7^1000",
        "0.1^1000^2",
        "9^9^9^9^9^9^9^9",
        "2^0.5^1000000",
        "9" * 200 + "+1",
        "1+" * 60 + "1",
        "-" * 99 + "1",
        "(" * 150 + "1+1" + ")" * 150,
    ],
)
def test_calculate_rejects(query):
    assert calculate(query) is None


def test_rejected_inputs_are_not_computed():
    start = time.perf_counter()
    for query in ["9^9^9^9", "10^1000*10^1000", "(10^500)^2", "2^999^999", "(" * 90 + "9^999" + ")" * 90 + "^9"]:
        assert calculate(query) is None
    assert time.perf_counter() - start < 0.1


def test_non_arithmetic_queries_pay_only_the_regex(monkeypatch):
    parse = MagicMock()
    monkeypatch.setattr("mwmbl.widgets.arithmetic.ast.parse", parse)
    for query in ["python tutorial", "weather in london", "10km in miles", "20% of 150"]:
        assert calculate(query) is None
    parse.assert_not_called()


class StubRanker:
    def __init__(self):
        self.queries = []

    def search(self, s, additional_results, use_external_search=True):
        self.queries.append(s)
        return [Document("Calculator", "https://example.com/calculator", f"A calculator, for {s}", 1.0)]


@pytest.fixture
def ranker():
    return StubRanker()


@pytest.fixture
def client(ranker):
    api = NinjaAPI(urls_namespace=f"test-arithmetic-{next(API_NUMBERS)}")
    search._register_search_v2(api, ranker, None)
    return TestClient(api)


def test_search_computes_arithmetic_and_shows_calculator_results(client, ranker):
    body = client.get("/?" + urlencode({"q": " 164*8 "})).json()

    assert body["widgets"] == [{"type": "calculation", "expression": "164*8", "result": "1312"}]
    assert body["query"] == " 164*8 "
    assert [r["url"] for r in body["results"]] == ["https://example.com/calculator"]
    assert ranker.queries == ["calculator"]


def test_calculator_results_are_ranked_once_an_hour(client, ranker, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(search.time, "monotonic", lambda: now[0])

    for query in ["1+1", "2*3", "4/5"]:
        client.get("/?" + urlencode({"q": query}))
    assert ranker.queries == ["calculator"]

    now[0] += search.CALCULATOR_RESULTS_TTL_SECONDS
    client.get("/?" + urlencode({"q": "1+1"}))
    assert ranker.queries == ["calculator", "calculator"]


def test_search_without_arithmetic_is_unchanged(client, ranker):
    body = client.get("/?" + urlencode({"q": "python tutorial"})).json()

    assert body["widgets"] == []
    assert ranker.queries == ["python tutorial"]


def test_division_by_zero_gives_ordinary_search(client, ranker):
    body = client.get("/?" + urlencode({"q": "1/0"})).json()

    assert body["widgets"] == []
    assert ranker.queries == ["1/0"]
