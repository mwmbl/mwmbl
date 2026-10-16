"""
The Guardian, NHS, Wikidata and homepage-guesser sources, and the recipe smoke check, against
canned HTTP responses: what each turns a response into, and that a failing upstream yields no
results rather than an exception.
"""

from types import SimpleNamespace

import httpx
import pytest

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.super_search_sources import guardian, homepage, nhs, smoke, wikidata

TRAIL = (
    "<p>"
    + "The prime minister said on Tuesday that the <b>government</b> would publish its plan for the economy. " * 4
    + "</p>"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


def _status(status: int):
    return lambda request: httpx.Response(status)


# ------------------------------------------------------------------- guardian


async def test_guardian_without_a_key_makes_no_request(settings):
    settings.GUARDIAN_API_KEY = ""

    def forbidden(request):
        raise AssertionError("no request expected without an API key")

    async with _client(forbidden) as client:
        assert await guardian.search(client, "news", 5) == []


async def test_guardian_results_become_documents_with_cleaned_trail_text(settings):
    settings.GUARDIAN_API_KEY = "key"
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "response": {
                    "results": [
                        {
                            "webTitle": "Headline",
                            "webUrl": "https://www.theguardian.com/a",
                            "fields": {"trailText": TRAIL},
                        },
                        {"webTitle": "No trail", "webUrl": "https://www.theguardian.com/b"},
                        {"webTitle": "", "webUrl": "https://www.theguardian.com/c"},
                    ]
                }
            },
        )

    async with _client(handler) as client:
        docs = await guardian.search(client, "election", 50)

    assert [d.url for d in docs] == ["https://www.theguardian.com/a", "https://www.theguardian.com/b"]
    assert "government" in docs[0].extract and "<" not in docs[0].extract
    assert docs[1].extract == ""
    assert seen["page-size"] == "10"
    assert seen["api-key"] == "key"


@pytest.mark.parametrize("handler", [_status(500), lambda request: httpx.Response(200, text="not json")])
async def test_guardian_failures_give_no_results(settings, handler):
    settings.GUARDIAN_API_KEY = "key"

    async with _client(handler) as client:
        assert await guardian.search(client, "election", 5) == []


# ------------------------------------------------------------------------ nhs

NHS_PAGE = """
<html><body>
<a class="app-search-results-item" href="/click?url=%2Fconditions%2Fasthma%2F&q=asthma">Asthma - NHS</a>
<a class="app-search-results-item" href="/conditions/cough/">Cough</a>
<a class="app-search-results-item" href="/conditions/cold/">Cold</a>
<a class="other" href="/ignored/">Ignored</a>
</body></html>
"""


async def test_nhs_results_use_the_tracked_target_and_respect_the_limit():
    async with _client(lambda request: httpx.Response(200, text=NHS_PAGE)) as client:
        docs = await nhs.search(client, "asthma", 2)

    assert [(d.title, d.url) for d in docs] == [
        ("Asthma - NHS", "https://www.nhs.uk/conditions/asthma/"),
        ("Cough", "https://www.nhs.uk/conditions/cough/"),
    ]


async def test_nhs_failure_gives_no_results():
    async with _client(_status(503)) as client:
        assert await nhs.search(client, "asthma", 5) == []


# ------------------------------------------------------------------- wikidata


def _wikidata_handler(search_hits, entities, entity_status: int = 200):
    def handler(request):
        if request.url.params["action"] == "wbsearchentities":
            return httpx.Response(200, json={"search": search_hits})
        return httpx.Response(entity_status, json={"entities": entities})

    return handler


def _entity(label, description, *urls):
    claims = [{"mainsnak": {"datavalue": {"value": url}}} for url in urls]
    return {
        "labels": {"en": {"value": label}},
        "descriptions": {"en": {"value": description}},
        "claims": {"P856": claims},
    }


async def test_wikidata_returns_one_official_site_per_entity_in_search_order():
    handler = _wikidata_handler(
        [{"id": "Q2"}, {"id": "Q1"}, {"id": "Q3"}],
        {
            "Q1": _entity("One", "first", "https://one.example/", "https://one-alt.example/"),
            "Q2": _entity("Two", "second", "https://two.example/"),
            "Q3": {"claims": {"P856": [{"mainsnak": {}}]}},
        },
    )

    async with _client(handler) as client:
        docs = await wikidata.search(client, "numbers", 5)

    assert [(d.title, d.url, d.extract) for d in docs] == [
        ("Two", "https://two.example/", "second"),
        ("One", "https://one.example/", "first"),
    ]


async def test_wikidata_with_no_matching_entities_makes_no_second_request():
    async with _client(_wikidata_handler([], {})) as client:
        assert await wikidata.search(client, "nothing", 5) == []


async def test_wikidata_failure_in_either_step_gives_no_results():
    async with _client(_status(500)) as client:
        assert await wikidata.search(client, "x", 5) == []

    handler = _wikidata_handler([{"id": "Q1"}], {}, entity_status=500)
    async with _client(handler) as client:
        assert await wikidata.search(client, "x", 5) == []


# ------------------------------------------------------------------- homepage


def _page(title: str, content_type: str = "text/html", status: int = 200):
    return httpx.Response(status, text=f"<html><head><title>{title}</title></head></html>", headers={
        "content-type": content_type
    })  # fmt: skip


async def test_homepage_guesser_keeps_pages_whose_title_mentions_the_query():
    def handler(request):
        host = request.url.host
        if host == "solvera.com":
            return _page("Solvera Jewellery")
        if host == "solvera.co.uk":
            return _page("Totally unrelated parking page")
        if host == "solvera.org":
            return _page("Solvera", content_type="application/pdf")
        return _page("Solvera", status=404)

    async with _client(handler) as client:
        docs = await homepage.search(client, "Solvera", 5)

    assert [(d.title, d.url) for d in docs] == [("Solvera Jewellery", "https://solvera.com/")]


async def test_homepage_guesser_stops_at_the_limit_and_skips_unreachable_hosts():
    def handler(request):
        if request.url.host == "brand.com":
            raise httpx.ConnectError("refused")
        return _page("Brand home")

    async with _client(handler) as client:
        docs = await homepage.search(client, "brand", 1)

    assert [d.url for d in docs] == ["https://brand.co.uk/"]


@pytest.mark.parametrize("query", ["ab", "a", "  ", "!!"])
async def test_homepage_guesser_ignores_queries_too_short_to_guess_from(query):
    def forbidden(request):
        raise AssertionError("no request expected")

    async with _client(forbidden) as client:
        assert await homepage.search(client, query, 5) == []


# ---------------------------------------------------------------------- smoke


def _docs(*urls_and_titles):
    return [Document(title=title, url=url, extract="") for url, title in urls_and_titles]


def _recipe(**smoke_config):
    return SimpleNamespace(smoke=smoke_config)


def _searching(monkeypatch, real, control):
    async def fake_search(client, recipe, query, limit):
        return real if query == "real query" else control

    monkeypatch.setattr(smoke, "search_with_recipe", fake_search)


def test_control_overlap_is_a_jaccard_of_result_urls():
    one = _docs(("a", "A"), ("b", "B"))
    two = _docs(("b", "B"), ("c", "C"))

    assert smoke.control_overlap(one, two) == pytest.approx(1 / 3)
    assert smoke.control_overlap(one, []) is None


async def test_a_recipe_without_smoke_settings_fails():
    ok, reason = await smoke.check_recipe(None, _recipe(query="real query"))

    assert not ok
    assert "missing" in reason


async def test_a_recipe_that_returns_nothing_fails(monkeypatch):
    _searching(monkeypatch, [], [])

    ok, reason = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="x"))

    assert (ok, reason) == (False, "no results for 'real query'")


async def test_a_recipe_whose_top_result_lacks_a_url_fails(monkeypatch):
    _searching(monkeypatch, _docs(("", "Title")), [])

    ok, reason = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="title"))

    assert not ok
    assert "missing url/title" in reason


async def test_a_recipe_with_no_matching_title_fails(monkeypatch):
    _searching(monkeypatch, _docs(("a", "Cats"), ("b", "Dogs")), [])

    ok, reason = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="pasta"))

    assert not ok
    assert "no title contains 'pasta'" in reason


async def test_a_recipe_returning_the_same_results_for_gibberish_is_scraping_boilerplate(monkeypatch):
    same = _docs(("a", "Pasta"), ("b", "Pasta bake"))
    _searching(monkeypatch, same, same)

    ok, reason = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="pasta"))

    assert not ok
    assert "barely change" in reason


async def test_a_query_sensitive_recipe_passes(monkeypatch):
    _searching(monkeypatch, _docs(("a", "Pasta")), _docs(("z", "Something else")))

    ok, reason = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="pasta"))

    assert (ok, reason) == (True, "")


async def test_an_erroring_control_search_does_not_fail_the_recipe(monkeypatch):
    async def fake_search(client, recipe, query, limit):
        if query == "real query":
            return _docs(("a", "Pasta"))
        raise RuntimeError("boom")

    monkeypatch.setattr(smoke, "search_with_recipe", fake_search)

    ok, _ = await smoke.check_recipe(None, _recipe(query="real query", expect_title_contains="pasta"))

    assert ok
