import itertools
import json
import socket
from copy import deepcopy
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from ninja import NinjaAPI
from ninja.testing import TestClient

from mwmbl.indexer.index_batches import index_widget_documents
from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentSource, TinyIndex
from mwmbl.tinysearchengine.rank import HeuristicRanker
from mwmbl.widgets.keys import storage_key
from mwmbl.widgets.lookup import WidgetFinder
from mwmbl.widgets.spec import WidgetSpec, load_specs

API_NUMBERS = itertools.count()

WEATHER = {
    "name": "weather",
    "site": "example.com",
    "source_name": "Example Weather",
    "page_url": "https://example.com/forecast?lat={lat}&lon={lon}",
    "api_url": "https://api.example.com/v1/forecast?latitude={lat}&longitude={lon}&units={units}",
    "api_host": "api.example.com",
    "inputs": [
        {"name": "lat", "type": "number", "pattern": r"(-?\d+(?:\.\d+)?)"},
        {"name": "lon", "type": "number"},
        {"name": "units", "type": "choice", "default": "metric", "label": "Units"},
    ],
    "outputs": [{"label": "Temperature", "path": "current.temperature_2m", "unit": "C"}],
    "display": "forecast",
    "trigger_word": "weather",
    "trigger_tokens": ["weather"],
    "cache_ttl_seconds": 600,
}

CALCULATOR = {
    **WEATHER,
    "name": "calculator",
    "page_url": "https://example.com/calc",
    "api_url": "https://api.example.com/calc",
    "inputs": [],
    "trigger_word": "calculator",
    "trigger_tokens": ["calculator"],
}


def make_spec(**changes) -> WidgetSpec:
    return WidgetSpec.model_validate({**deepcopy(WEATHER), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"api_url": "http://api.example.com/v1"},
        {"api_url": "https://evil.example.org/v1?lat={lat}"},
        {"api_url": "https://api.example.com:8443/v1"},
        {"api_url": "https://127.0.0.1/v1", "api_host": "127.0.0.1"},
        {"api_url": "https://{units}.example.com/v1"},
        {"page_url": "https://other.example.org/forecast?lat={lat}&lon={lon}"},
        {"trigger_word": "forecast"},
        {"outputs": [{"label": "x", "path": "a..b"}]},
        {"inputs": [{"name": "lat", "pattern": "(a"}, {"name": "lon"}, {"name": "units"}]},
        {"inputs": [{"name": "lat", "pattern": "a|b"}, {"name": "lon"}, {"name": "units"}]},
        {"inputs": [{"name": "lat"}, {"name": "lat"}, {"name": "lon"}, {"name": "units"}]},
    ],
)
def test_bad_specs_are_rejected(changes):
    with pytest.raises(ValueError):
        make_spec(**changes)


def test_page_url_template_is_matched_to_inputs():
    spec = make_spec()
    assert spec.match_page_url("https://example.com/forecast?lat=51.5&lon=-0.13") == {"lat": "51.5", "lon": "-0.13"}
    assert spec.match_page_url("https://example.org/forecast?lat=51.5&lon=-0.13") is None


def test_load_specs_reads_json_files_and_rejects_bad_ones(tmp_path):
    assert load_specs(tmp_path / "missing") == []
    (tmp_path / "b.json").write_text(json.dumps(CALCULATOR))
    (tmp_path / "a.json").write_text(json.dumps(WEATHER))
    assert [spec.name for spec in load_specs(tmp_path)] == ["weather", "calculator"]

    (tmp_path / "c.json").write_text(json.dumps(CALCULATOR))
    with pytest.raises(ValueError, match="Duplicate"):
        load_specs(tmp_path)


def widget_document(title, lat, tokens):
    """A forecast widget for a place, filed under its trigger word and its last token."""
    url = f"https://example.com/forecast?lat={lat}&lon=1"
    anchor = sorted(tokens - {"weather"}, key=title.lower().index)[-1]
    return Document(title, url, " ".join(sorted(tokens)), term=storage_key(tokens, "weather", anchor))


@pytest.fixture
def index_path(tmp_path):
    path = str(tmp_path / "index.tinysearch")
    TinyIndex.create(Document, path, num_pages=64, page_size=PAGE_SIZE)
    return path


def store(index_path, documents):
    index_widget_documents([(d, d.term) for d in documents], index_path)


FORECASTS = [
    widget_document("York weather, UK", 53, {"york", "weather"}),
    widget_document("New York weather, US", 40, {"new", "york", "weather"}),
    widget_document("Paris weather", 48, {"paris", "weather"}),
]
CALC = Document(
    "Calculator", "https://example.com/calc", "calculator", term="widget:calculator", source=DocumentSource.WIDGET
)


@pytest.fixture
def tiny_index(index_path):
    store(index_path, [*FORECASTS, CALC])
    with TinyIndex(Document, index_path) as index:
        yield index


SPECS = [make_spec(), WidgetSpec.model_validate(CALCULATOR)]


@pytest.mark.parametrize(
    "query, titles",
    [
        ("weather in york", ["York weather, UK"]),
        ("york weather today", ["York weather, UK"]),
        ("weather in new york", ["New York weather, US"]),
        ("alpha beta gamma delta epsilon zeta eta york weather", ["York weather, UK"]),
        ("weather", []),
        ("weather in london", []),
        ("calculator", ["Calculator"]),
        ("weather york calculator", ["York weather, UK", "Calculator"]),
    ],
)
def test_find_widgets(tiny_index, query, titles):
    assert [w.title for w in WidgetFinder(SPECS).find(query, tiny_index)] == titles


def test_only_widget_documents_whose_url_fits_their_spec_count(index_path):
    key = FORECASTS[0].term
    crawled = Document("Crawled", FORECASTS[0].url, "york weather", term=key)
    wrong_host = Document(
        "Evil", "https://evil.example.org/forecast?lat=1&lon=1", "weather york", term=key, source=DocumentSource.WIDGET
    )
    with TinyIndex(Document, index_path, "w") as index:
        # A crawled page containing the text of a widget key is stored under that very term.
        index.store_in_page(index.get_key_page_index(key), [crawled, wrong_host])
    with TinyIndex(Document, index_path) as index:
        assert [d.title for d in index.retrieve(key)] == ["Crawled", "Evil"]
        assert WidgetFinder(SPECS).find("york weather", index) == []


def test_a_query_without_a_trigger_word_reads_no_keys():
    index = MagicMock()
    assert WidgetFinder(SPECS).find("python tutorial", index) == []
    assert WidgetFinder([]).find("weather york", index) == []
    index.retrieve.assert_not_called()


def test_a_trigger_word_costs_at_most_21_key_reads():
    index = MagicMock()
    index.retrieve.return_value = []
    WidgetFinder(SPECS).find("weather " + " ".join(f"word{i}" for i in range(30)), index)
    assert index.retrieve.call_count == 21


class StubRanker:
    def __init__(self):
        self.queries = []

    def search(self, s, additional_results, use_external_search=True):
        self.queries.append(s)
        return []


@pytest.mark.django_db
def test_search_returns_stubs_with_no_outbound_call_or_query(tiny_index, monkeypatch, django_assert_num_queries):
    def no_network(*args, **kwargs):
        raise AssertionError("Search made an outbound connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(search, "default_finder", lambda: WidgetFinder(SPECS))
    api = NinjaAPI(urls_namespace=f"test-lookup-{next(API_NUMBERS)}")
    ranker = StubRanker()
    search._register_search_v2(api, ranker, tiny_index)
    client = TestClient(api)

    with django_assert_num_queries(0):
        body = client.get("/?" + urlencode({"q": "weather in york"})).json()
        plain = client.get("/?" + urlencode({"q": "python tutorial"})).json()

    stub = {"type": "widget", "title": "York weather, UK", "url": FORECASTS[0].url, "source_name": "Example Weather"}
    assert body["widgets"] == [stub]
    assert ranker.queries == ["weather in york", "python tutorial"]
    assert plain["widgets"] == []


def test_a_tie_goes_to_the_document_stored_first(index_path):
    store(index_path, [FORECASTS[0], widget_document("York again", 1, {"york", "weather"})])
    with TinyIndex(Document, index_path) as index:
        assert [w.title for w in WidgetFinder(SPECS).find("york weather", index)] == ["York weather, UK"]


def test_ordinary_retrieval_leaves_out_widget_documents():
    index = MagicMock()
    index.retrieve.return_value = [CALC, Document("Page", "https://example.org/", "calculator")]
    ranker = HeuristicRanker(index, MagicMock(complete=lambda term: []))
    assert [page.title for page in ranker.retrieve("widget:calculator").pages] == ["Page"]
