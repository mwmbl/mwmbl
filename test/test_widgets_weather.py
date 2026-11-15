import json

import pytest
from django.core.management import CommandError, call_command

from mwmbl.management.commands import index_widgets
from mwmbl.management.commands.index_widgets import sync_widgets
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentSource, TinyIndex
from mwmbl.widgets.api import input_values, output_fields
from mwmbl.widgets.lookup import WidgetFinder
from mwmbl.widgets.places import read_countries, read_places
from mwmbl.widgets.spec import PROVIDERS_DIR, load_specs
from mwmbl.widgets.wmo import WMO_CODES

SPECS = [spec for spec in load_specs(PROVIDERS_DIR) if spec.site == "open-meteo.com"]
WEATHER, FORECAST = SPECS
CALCULATOR = next(spec for spec in load_specs(PROVIDERS_DIR) if spec.site != "open-meteo.com")
TRIGGER_WORDS = {spec.trigger_word for spec in SPECS}

COUNTRIES = "#iso\tiso3\tnum\tfips\tCountry\n" + "\n".join(
    f"{code}\t{code}X\t0\t{code}\t{name}" for code, name in [("GB", "United Kingdom"), ("US", "United States")]
)


def city(geoname_id, name, ascii_name, latitude, longitude, country, division, population):
    columns = [str(geoname_id), name, ascii_name, "", latitude, longitude, "P", "PPL", country, "", division]
    return "\t".join(columns + ["", "", "", str(population), "", "", "UTC", "2024-01-01"])


# York and New York share a token; two Springfields share a name and country; Zürich is written with an umlaut.
CITIES = "\n".join(
    [
        city(1, "York", "York", "53.95763", "-1.08271", "GB", "ENG", 152841),
        city(2, "New York City", "New York City", "40.71427", "-74.00597", "US", "NY", 8804190),
        city(3, "Springfield", "Springfield", "37.21533", "-93.29824", "US", "MO", 169176),
        city(4, "Springfield", "Springfield", "39.80172", "-89.64371", "US", "IL", 114394),
        city(5, "Zürich", "Zurich", "47.36667", "8.55", "GB", "ZH", 341730),
        city(6, "Dup", "Dup", "1.1", "2.2", "GB", "ENG", 20000),
        city(7, "Dup", "Dup", "3.3", "4.4", "GB", "ENG", 19000),
        city(8, "Weather", "Weather", "5.5", "6.6", "GB", "ENG", 16000),
    ]
)

# Cut down from a real answer of https://api.open-meteo.com/v1/forecast?...&forecast_days=4&timezone=auto
ANSWER = {
    "current": {"time": "2026-11-13T12:00", "temperature_2m": 8.4, "weather_code": 3, "wind_speed_10m": 12.3},
    "daily": {
        "time": ["2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16"],
        "weather_code": [3, 61, 95, 1234],
        "temperature_2m_max": [9.1, 10.2, 8.8, 7.0],
        "temperature_2m_min": [4.1, 5.5, 3.2, 1.0],
    },
}


@pytest.fixture
def places(tmp_path):
    cities_path, countries_path = tmp_path / "cities.txt", tmp_path / "countries.txt"
    cities_path.write_text(CITIES)
    countries_path.write_text(COUNTRIES)
    return read_places(cities_path, read_countries(countries_path), TRIGGER_WORDS)


@pytest.fixture
def index_path(tmp_path):
    path = str(tmp_path / "index.tinysearch")
    TinyIndex.create(Document, path, num_pages=64, page_size=PAGE_SIZE)
    return path


def find(index_path, query):
    with TinyIndex(Document, index_path) as index:
        return WidgetFinder(SPECS).find(query, index)


def widget_documents(index_path):
    with TinyIndex(Document, index_path) as index:
        return [d for i in range(index.num_pages) for d in index.get_page(i) if d.source == DocumentSource.WIDGET]


def test_specs_credit_open_meteo_and_do_not_look_like_the_real_page():
    for spec in SPECS:
        assert spec.source_name == "Open-Meteo.com"
        assert spec.display == "forecast"
        assert spec.cache_ttl_seconds == 1800
        assert spec.match_page_url(spec.page_url.format(latitude="1.5", longitude="-2")) == {
            "latitude": "1.5",
            "longitude": "-2",
        }
        assert spec.page_url.endswith("&ref=mwmbl") or spec.page_url.endswith("&ref=mwmbl-forecast")
        assert spec.match_page_url("https://open-meteo.com/en/docs?latitude=1.5&longitude=-2") is None
    assert WEATHER.page_url != FORECAST.page_url


def test_places_are_most_populous_first_with_unique_titles(places):
    assert [(p.name, p.country) for p in places] == [
        ("New York City", "United States"),
        ("Zürich", "United Kingdom"),
        ("Springfield (MO)", "United States"),
        ("York", "United Kingdom"),
        ("Springfield (IL)", "United States"),
        ("Dup (ENG)", "United Kingdom"),
    ]
    assert places[1].tokens == ("zurich",)
    # The second Dup shares its name, country and division; "Weather" has no token left to name it by.


def test_values_come_from_the_url_and_the_answer_is_words(places, index_path):
    sync_widgets(index_path, SPECS, lambda spec: True, set(), places)
    [stub] = find(index_path, "weather in York")
    assert stub.title == "York weather, United Kingdom"
    assert stub.source_name == "Open-Meteo.com"
    assert input_values(WEATHER, stub.url, "weather in york") == {"latitude": "53.95763", "longitude": "-1.08271"}

    fields = {f.label: f for f in output_fields(WEATHER, ANSWER)}
    assert len(fields) == 3 + 3 * 4 - 1  # now and three days, nothing more; the unknown code is gone
    assert fields["Conditions"].value == "Overcast"
    assert (fields["Temperature"].value, fields["Temperature"].unit) == ("8.4", "°C")
    assert [fields[f"Day {d} conditions"].value for d in (1, 2)] == ["Slight rain", "Thunderstorm"]
    assert fields["Day 1"].value == "2026-11-14"
    assert "Day 3 conditions" not in fields  # an unknown code is dropped rather than shown as a number
    assert all(code in WMO_CODES for code in (0, 3, 61, 95))


def test_weather_and_forecast_find_the_place_and_the_longest_name_wins(places, index_path):
    sync_widgets(index_path, SPECS, lambda spec: True, set(), places)
    assert [s.title for s in find(index_path, "weather york")] == ["York weather, United Kingdom"]
    assert [s.title for s in find(index_path, "York forecast")] == ["York forecast, United Kingdom"]
    assert [s.title for s in find(index_path, "new york city weather")] == ["New York City weather, United States"]
    assert [s.title for s in find(index_path, "weather in new york")] == ["New York City weather, United States"]
    assert [s.title for s in find(index_path, "york weather forecast")] == ["York weather, United Kingdom"]
    assert [s.title for s in find(index_path, "weather in springfield")] == ["Springfield (MO) weather, United States"]
    assert [s.title for s in find(index_path, "zurich weather")] == ["Zürich weather, United Kingdom"]
    for query in ["weather", "forecast", "york", "weather atlantis"]:
        assert find(index_path, query) == []


def test_sync_removes_places_that_are_gone_or_blocked(places, index_path):
    keys = sync_widgets(index_path, SPECS, lambda spec: True, set(), places)
    assert len(widget_documents(index_path)) == 2 * len(places)

    keys = sync_widgets(index_path, SPECS, lambda spec: True, keys, places[:2])
    assert len(widget_documents(index_path)) == 4
    assert find(index_path, "weather york") == []

    keys = sync_widgets(index_path, SPECS, lambda spec: spec is WEATHER, keys, places[:2])
    assert {d.title for d in widget_documents(index_path)} == {
        "New York City weather, United States",
        "Zürich weather, United Kingdom",
    }
    assert sync_widgets(index_path, SPECS, lambda spec: True, keys, []) == set()
    assert widget_documents(index_path) == []


def test_command_reads_places_and_skips_api_robots_for_open_meteo(tmp_path, index_path, settings, mocker, redis_cache):
    settings.DATA_PATH = str(tmp_path)
    settings.INDEX_NAME = "index.tinysearch"
    (tmp_path / "cities.txt").write_text(CITIES)
    (tmp_path / "countries.txt").write_text(COUNTRIES)
    mocker.patch.object(index_widgets, "load_specs", return_value=SPECS)
    robots = mocker.patch.object(index_widgets, "robots_allowed", return_value=True)
    call_command("index_widgets", places=[tmp_path / "cities.txt", tmp_path / "countries.txt"])
    assert len(widget_documents(index_path)) == 12
    assert {call.args[0] for call in robots.call_args_list} == {
        "https://open-meteo.com/en/docs?latitude=0&longitude=0&ref=mwmbl",
        "https://open-meteo.com/en/docs?latitude=0&longitude=0&ref=mwmbl-forecast",
    }
    place_keys = json.loads((tmp_path / "widget-place-keys.json").read_text())
    assert len(place_keys) > 0
    assert json.loads((tmp_path / "widget-keys.json").read_text()) == []

    call_command("index_widgets")
    assert len(widget_documents(index_path)) == 12
    assert json.loads((tmp_path / "widget-place-keys.json").read_text()) == place_keys

    call_command("index_widgets", no_places=True)
    assert widget_documents(index_path) == []
    assert json.loads((tmp_path / "widget-place-keys.json").read_text()) == []


def test_a_plain_run_keeps_places_while_it_syncs_the_other_widgets(tmp_path, index_path, settings, mocker, redis_cache):
    settings.DATA_PATH = str(tmp_path)
    settings.INDEX_NAME = "index.tinysearch"
    (tmp_path / "cities.txt").write_text(CITIES)
    (tmp_path / "countries.txt").write_text(COUNTRIES)
    mocker.patch.object(index_widgets, "load_specs", return_value=[*SPECS, CALCULATOR])
    robots = mocker.patch.object(index_widgets, "robots_allowed", return_value=True)
    call_command("index_widgets", places=[tmp_path / "cities.txt", tmp_path / "countries.txt"])
    assert len(widget_documents(index_path)) == 13

    robots.return_value = False
    call_command("index_widgets")
    assert len(widget_documents(index_path)) == 12
    assert json.loads((tmp_path / "widget-keys.json").read_text()) == []


def test_places_and_no_places_cannot_be_combined(tmp_path):
    with pytest.raises(CommandError):
        call_command("index_widgets", "--no-places", "--places", "a", "b")
