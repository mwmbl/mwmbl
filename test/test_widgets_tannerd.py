import json
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command

from mwmbl.crawler.retrieve import RobotsUnavailable
from mwmbl.indexer.index_batches import index_widget_documents
from mwmbl.management.commands import index_widgets
from mwmbl.management.commands.index_widgets import sync_widgets, widget_document
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentSource, TinyIndex
from mwmbl.widgets.api import input_values, output_fields
from mwmbl.widgets.lookup import WidgetFinder
from mwmbl.widgets.openapi_import import draft_spec
from mwmbl.widgets.spec import PROVIDERS_DIR, WidgetSpec, load_specs

SPECS = {spec.name: spec for spec in load_specs(PROVIDERS_DIR) if spec.site == "tannerd.net"}

# Real answers of https://tannerd.net/api/v1/..., cut down to `result`, and the query each is for.
CASES = [
    (
        "percentage-change-calculator",
        "percentage change from 100 to 125",
        {"original": "100", "new_value": "125"},
        {"result": {"difference": 25, "percentage_change": 25, "direction": "increase"}},
        ["25", "25", "increase"],
    ),
    (
        "date-difference-calculator",
        "days between 2026-09-01 and 2026-09-30",
        {"start_date": "2026-09-01", "end_date": "2026-09-30"},
        {"result": {"signed_days": 29, "absolute_days": 29, "end_is_on_or_after_start": True}},
        ["29"],
    ),
    (
        "salary-to-hourly-calculator",
        "60000 salary to hourly 40 hours",
        {"annual_salary": "60000", "hours_per_week": "40"},
        {"result": {"hourly_equivalent": 28.8462, "weekly_equivalent": 1153.85, "monthly_equivalent": 5000}},
        ["28.8462", "1153.85", "5000"],
    ),
    (
        "trip-cost-calculator",
        "trip cost 300 miles at 30 mpg and $3.25 a gallon",
        {"miles": "300", "mpg": "30", "gas_price": "3.25"},
        {"result": {"gallons_needed": 10, "gas_cost": 32.5, "trip_cost_with_tolls": 32.5, "cost_per_mile": 0.1083}},
        ["32.5", "10", "0.1083"],
    ),
    (
        "electricity-cost-calculator",
        "electricity cost 1500 watts 5 hours a day at $0.18 per kwh",
        {"watts": "1500", "hours_per_day": "5", "rate_per_kwh": "0.18"},
        {"result": {"kwh_per_month": 225, "cost_per_day": 1.35, "cost_per_month": 40.5, "cost_per_year": 492.75}},
        ["1.35", "40.5", "492.75", "225"],
    ),
]

NOT_TRIGGERED = [
    "percentage change",
    "salary",
    "salary to hourly calculator",
    "trip cost",
    "electricity cost",
    "days between dates",
    "change 100 to 125",
    "widget:calculator",
]


@pytest.fixture
def index_path(tmp_path):
    path = str(tmp_path / "index.tinysearch")
    TinyIndex.create(Document, path, num_pages=64, page_size=PAGE_SIZE)
    return path


def find(index_path, query):
    with TinyIndex(Document, index_path) as index:
        return WidgetFinder(list(SPECS.values())).find(query, index)


def widget_documents(index_path):
    with TinyIndex(Document, index_path) as index:
        return [d for i in range(index.num_pages) for d in index.get_page(i) if d.source == DocumentSource.WIDGET]


def test_the_five_calculators_are_checked_in():
    assert len(SPECS) == 5
    documents = [widget_document(spec) for spec in SPECS.values()]
    assert len({d.title for d in documents}) == len(documents)
    assert len({d.term for d in documents}) == len(documents)
    for spec, document in zip(SPECS.values(), documents):
        assert urlsplit(document.url).netloc == "tannerd.net"
        assert spec.match_page_url(document.url) == {}
        assert document.extract.split() == spec.trigger_tokens


@pytest.mark.parametrize("name, query, values, answer, fields", CASES)
def test_each_calculator_is_triggered_and_read(index_path, name, query, values, answer, fields):
    sync_widgets(index_path, list(SPECS.values()), lambda spec: True, set())
    spec = SPECS[name]
    stubs = find(index_path, query)
    assert [stub.url for stub in stubs] == [spec.page_url]
    assert input_values(spec, spec.page_url, query) == values
    assert [field.value for field in output_fields(spec, answer)] == fields


@pytest.mark.parametrize("query", NOT_TRIGGERED)
def test_unrelated_queries_trigger_nothing(index_path, query):
    sync_widgets(index_path, list(SPECS.values()), lambda spec: True, set())
    assert find(index_path, query) == []


@pytest.mark.parametrize(
    "query, spec_name",
    [
        ("percentage change from 1,000 to 1,250", "percentage-change-calculator"),
        ("monthly salary 5000 per month to hourly", "salary-to-hourly-calculator"),
        ("salary 60k to hourly", "salary-to-hourly-calculator"),
        ("electricity cost 18 cents per kwh 1500 watts 5 hours", "electricity-cost-calculator"),
    ],
)
def test_ambiguous_values_are_not_guessed(query, spec_name):
    with pytest.raises(Exception, match="422|Invalid|No value"):
        input_values(SPECS[spec_name], SPECS[spec_name].page_url, query)


def test_sync_removes_blocked_and_stale_widgets_and_keeps_crawled_documents(index_path):
    specs = list(SPECS.values())
    crawled = Document("Crawled", "https://example.com/crawled", "text", term=widget_document(specs[0]).term)
    with TinyIndex(Document, index_path, "w") as index:
        with index.page(index.get_key_page_index(crawled.term)) as page:
            page.store([crawled])
    stale = Document("Gone, example.org", "https://example.org/tools/gone/", "gone calc", term="widget:calc gone")
    index_widget_documents([(stale, stale.term)], index_path)
    assert len(widget_documents(index_path)) == 1

    keys = sync_widgets(index_path, specs, lambda spec: True, set())
    assert len(keys) == 5
    assert len(widget_documents(index_path)) == 6  # a key no run has recorded is not known to be stale

    previous = {"widget:calc gone"}
    keys = sync_widgets(index_path, specs, lambda spec: True, previous)
    assert len(keys) == 5 and len(widget_documents(index_path)) == 5
    assert previous == keys | {"widget:calc gone"}
    assert sync_widgets(index_path, specs, lambda spec: True, keys) == keys  # idempotent

    blocked = "date-difference-calculator"
    keys = sync_widgets(index_path, specs, lambda spec: spec.name != blocked, keys)
    assert len(keys) == 4
    assert {d.url for d in widget_documents(index_path)} == {s.page_url for s in specs if s.name != blocked}
    assert find(index_path, CASES[1][1]) == []
    with TinyIndex(Document, index_path) as index:
        assert [d.title for d in index.retrieve(crawled.term) if d.source is None] == ["Crawled"]


def test_site_needs_robots_to_allow_page_and_api(mocker):
    spec = SPECS["trip-cost-calculator"]
    robots = mocker.patch.object(index_widgets, "robots_allowed", return_value=True)
    assert index_widgets.site_allows_mwmbl(spec, None)
    assert robots.call_args.args[0] == "https://tannerd.net/api/v1/trip-cost?miles=0&mpg=0&gas_price=0"
    robots.side_effect = [True, False]
    assert not index_widgets.site_allows_mwmbl(spec, None)
    robots.side_effect = RobotsUnavailable
    assert not index_widgets.site_allows_mwmbl(spec, None)


def test_command_indexes_and_purges_a_blocked_site(tmp_path, index_path, settings, mocker, redis_cache):
    settings.DATA_PATH = str(tmp_path)
    settings.INDEX_NAME = "index.tinysearch"
    mocker.patch.object(index_widgets, "load_specs", return_value=list(SPECS.values()))
    robots = mocker.patch.object(index_widgets, "robots_allowed", return_value=True)
    call_command("index_widgets")
    assert len(widget_documents(index_path)) == 5
    assert len(json.loads((tmp_path / "widget-keys.json").read_text())) == 5

    robots.return_value = False
    call_command("index_widgets")
    assert widget_documents(index_path) == []
    assert json.loads((tmp_path / "widget-keys.json").read_text()) == []


def test_draft_spec_from_an_openapi_operation():
    openapi = {
        "servers": [{"url": "https://calc.example.org"}],
        "paths": {
            "/api/v1/trip-cost": {
                "get": {
                    "parameters": [
                        {"name": "miles", "in": "query", "schema": {"type": "number"}, "example": 300},
                        {"name": "driver_name", "in": "query", "schema": {"type": "string"}},
                    ]
                }
            }
        },
    }
    sample = {"ok": True, "result": {"gas_cost": 32.5, "nested": {"a": [1]}, "odd key": 2, "none": None}}
    draft = draft_spec(openapi, "/api/v1/trip-cost", sample)
    assert draft["inputs"] == [
        {"name": "miles", "type": "number", "label": "Miles", "default": "300"},
        {"name": "driver_name", "type": "text", "label": "Driver name"},
    ]
    assert draft["api_url"] == "https://calc.example.org/api/v1/trip-cost?miles={miles}&driver_name={driver_name}"
    assert [output["path"] for output in draft["outputs"]] == ["result.gas_cost", "result.nested.a[0]"]
    draft.update(page_url="https://calc.example.org/trip/", trigger_word="trip", trigger_tokens=["trip", "cost"])
    assert WidgetSpec.model_validate(draft).api_host == "calc.example.org"
