"""Tests for the declarative YAML recipe engine (recipe.py).

Each recipe is exercised against a mocked httpx transport, asserting the
url/title/extract coercion (templated URLs, relative-href resolution, HTML
stripping) and that an HTTP error returns [] rather than propagating. We also
verify the shipped recipe files load and register into SOURCES.
"""
import re
from pathlib import Path

import httpx
import pytest

from mwmbl.tinysearchengine.super_search_sources.recipe import (
    RECIPES_DIR,
    Recipe,
    load_recipes,
    search_with_recipe,
)

RECIPES = load_recipes()

# A minimal but complete recipe, used to check that valid recipes still load
# alongside a malformed one.
GOOD_RECIPE_YAML = """\
name: good
domain: example.com
field: other
request:
  url: https://example.com/api
  params:
    q: "{query}"
response:
  format: json
  results: results
  fields:
    title: title
    url: url
smoke:
  query: x
  expect_title_contains: x
"""


def _recipe(name: str) -> Recipe:
    return RECIPES[name]


# ---------------------------------------------------------------------------
# Recipe loading / registration
# ---------------------------------------------------------------------------

def test_shipped_recipes_load():
    assert {"wiktionary", "archive_org", "gutenberg"} <= set(RECIPES)
    assert _recipe("gutenberg").response_format == "html"


def test_recipes_registered_in_sources():
    from mwmbl.tinysearchengine.super_search_sources import SOURCES
    assert "wiktionary" in SOURCES and "gutenberg" in SOURCES


def test_load_recipes_skips_malformed(tmp_path, caplog):
    """A malformed recipe is logged and skipped, not raised — so one bad file
    can't crash the whole app at import time. Valid recipes still load."""
    (tmp_path / "broken.yaml").write_text("name: x\nrequest: {}\n")  # missing keys
    (tmp_path / "good.yaml").write_text(GOOD_RECIPE_YAML)
    with caplog.at_level("WARNING"):
        recipes = load_recipes(tmp_path)
    assert set(recipes) == {"good"}
    assert "broken.yaml" in caplog.text


@pytest.mark.parametrize("recipe", RECIPES.values(), ids=lambda r: r.name)
def test_recipe_has_smoke_block(recipe):
    assert recipe.smoke and recipe.smoke["query"] and recipe.smoke["expect_title_contains"]


# ---------------------------------------------------------------------------
# JSON: Wiktionary (MediaWiki) — templated URL + HTML-stripped snippet
# ---------------------------------------------------------------------------

async def test_wiktionary_templates_url_and_strips_html(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://en\.wiktionary\.org/w/api\.php.*"),
        json={"query": {"search": [
            {"title": "serendipity",
             "snippet": 'fortunate <span class="searchmatch">serendipity</span>'},
        ]}},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, _recipe("wiktionary"), "serendipity", 5)
    assert len(docs) == 1
    assert docs[0].url == "https://en.wiktionary.org/wiki/serendipity"
    assert docs[0].title == "serendipity"
    assert "<span" not in docs[0].extract and "serendipity" in docs[0].extract


async def test_wiktionary_quotes_titles_with_spaces(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://en\.wiktionary\.org/.*"),
        json={"query": {"search": [{"title": "United States", "snippet": "x"}]}},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, _recipe("wiktionary"), "united", 5)
    assert docs[0].url == "https://en.wiktionary.org/wiki/United%20States"


# ---------------------------------------------------------------------------
# JSON: archive.org — nested results path + templated URL from identifier
# ---------------------------------------------------------------------------

async def test_archive_org_nested_results_and_template(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://archive\.org/advancedsearch\.php.*"),
        json={"response": {"docs": [
            {"identifier": "apollo11", "title": "Apollo 11", "description": "Moon landing"},
            {"title": "no id"},  # no identifier -> no URL -> skipped
        ]}},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, _recipe("archive_org"), "apollo 11", 5)
    assert len(docs) == 1
    assert docs[0].url == "https://archive.org/details/apollo11"
    assert docs[0].extract == "Moon landing"


async def test_json_base_url_joins_relative_path(httpx_mock):
    """A JSON API returning a site-relative url (e.g. gov.uk's "/contact-hmrc") is
    joined onto base_url to the canonical absolute URL, without %-quoting slashes."""
    recipe = Recipe(
        name="govuk", domain="www.gov.uk", field="law-government",
        request={"url": "https://www.gov.uk/api/search.json", "params": {"q": "{query}"}},
        response={"format": "json", "base_url": "https://www.gov.uk", "results": "results",
                  "fields": {"title": "title", "url": "link"}},
        smoke={"query": "vat", "expect_title_contains": "VAT"},
    )
    httpx_mock.add_response(
        url=re.compile(r"https://www\.gov\.uk/api/search\.json.*"),
        json={"results": [{"title": "Contact HMRC", "link": "/find-hmrc-contacts/income-tax"}]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, recipe, "hmrc", 5)
    assert len(docs) == 1
    assert docs[0].url == "https://www.gov.uk/find-hmrc-contacts/income-tax"


# ---------------------------------------------------------------------------
# HTML: Project Gutenberg — CSS selectors + relative href resolution
# ---------------------------------------------------------------------------

GUTENBERG_HTML = """
<html><body><ul>
  <li class="booklink">
    <a class="link" href="/ebooks/84">
      <span class="title">Frankenstein</span>
      <span class="subtitle">Mary Wollstonecraft Shelley</span>
    </a>
  </li>
  <li class="other"><a class="link" href="/ignored">nope</a></li>
</ul></body></html>
"""


async def test_gutenberg_scrapes_and_resolves_relative_url(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://www\.gutenberg\.org/ebooks/search/.*"),
        text=GUTENBERG_HTML,
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, _recipe("gutenberg"), "frankenstein", 5)
    assert len(docs) == 1
    assert docs[0].url == "https://www.gutenberg.org/ebooks/84"
    assert docs[0].title == "Frankenstein"
    assert "Shelley" in docs[0].extract


# ---------------------------------------------------------------------------
# XML: schema completeness (no XML recipe ships, but the engine supports it)
# ---------------------------------------------------------------------------

BGG_XML = """<?xml version="1.0" encoding="utf-8"?>
<items>
  <item type="boardgame" id="13">
    <name type="primary" value="Catan"/>
  </item>
</items>
"""


async def test_xml_attributes_and_template(httpx_mock):
    recipe = Recipe(
        name="bgg",
        request={"url": "https://boardgamegeek.com/xmlapi2/search"},
        response={
            "format": "xml",
            "results": ".//item",
            "fields": {
                "title": {"selector": "name", "attr": "value"},
                "url": {"template": "https://boardgamegeek.com/boardgame/{id}"},
            },
        },
    )
    httpx_mock.add_response(url=re.compile(r"https://boardgamegeek\.com/.*"), text=BGG_XML)
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, recipe, "catan", 5)
    assert len(docs) == 1
    assert docs[0].title == "Catan"
    assert docs[0].url == "https://boardgamegeek.com/boardgame/13"


# ---------------------------------------------------------------------------
# Robustness: errors swallowed
# ---------------------------------------------------------------------------

async def test_http_error_returns_empty(httpx_mock):
    httpx_mock.add_response(status_code=500)
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, _recipe("wiktionary"), "x", 5)
    assert docs == []


def test_recipes_dir_exists():
    assert Path(RECIPES_DIR).is_dir()


# ---------------------------------------------------------------------------
# v2 engine enhancements: headers, POST json body, XML namespaces, list index
# ---------------------------------------------------------------------------

async def test_request_headers_sent(httpx_mock):
    recipe = Recipe(
        name="hdr",
        request={
            "url": "https://example.com/api",
            "headers": {"User-Agent": "custom-agent", "Accept": "application/json"},
            "params": {"q": "{query}"},
        },
        response={"format": "json", "results": "results", "fields": {"title": "t", "url": "u"}},
    )
    httpx_mock.add_response(url=re.compile(r"https://example\.com/.*"), json={"results": []})
    async with httpx.AsyncClient() as client:
        await search_with_recipe(client, recipe, "x", 5)
    request = httpx_mock.get_requests()[0]
    assert request.headers["user-agent"] == "custom-agent"
    assert request.headers["accept"] == "application/json"


async def test_post_json_body_with_substitution(httpx_mock):
    recipe = Recipe(
        name="post",
        request={
            "url": "https://example.com/search",
            "method": "POST",
            "json": {"query": "{query}", "size": "{limit}"},
        },
        response={"format": "json", "results": "hits", "fields": {"title": "title", "url": "url"}},
    )
    httpx_mock.add_response(
        url=re.compile(r"https://example\.com/.*"),
        json={"hits": [{"title": "Hit", "url": "https://example.com/1"}]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, recipe, "neutrino", 7)
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    import json as _json
    body = _json.loads(request.content)
    assert body == {"query": "neutrino", "size": "7"}
    assert docs[0].url == "https://example.com/1"


async def test_json_list_index_path(httpx_mock):
    recipe = Recipe(
        name="listidx",
        request={"url": "https://example.com/api", "params": {"q": "{query}"}},
        response={"format": "json", "results": "data.0.items",
                  "fields": {"title": "name", "url": "link"}},
    )
    httpx_mock.add_response(
        url=re.compile(r"https://example\.com/.*"),
        json={"data": [{"items": [{"name": "First", "link": "https://example.com/a"}]}]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, recipe, "x", 5)
    assert len(docs) == 1 and docs[0].title == "First"


ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Deep Learning</title>
    <summary>A survey</summary>
    <id>http://arxiv.org/abs/1234.5678</id>
  </entry>
</feed>
"""


async def test_xml_namespaces(httpx_mock):
    recipe = Recipe(
        name="atom",
        request={"url": "https://export.arxiv.org/api/query"},
        response={
            "format": "xml",
            "namespaces": {"atom": "http://www.w3.org/2005/Atom"},
            "results": ".//atom:entry",
            "fields": {"title": "atom:title", "extract": "atom:summary", "url": "atom:id"},
        },
    )
    httpx_mock.add_response(url=re.compile(r"https://export\.arxiv\.org/.*"), text=ATOM_XML)
    async with httpx.AsyncClient() as client:
        docs = await search_with_recipe(client, recipe, "deep learning", 5)
    assert len(docs) == 1
    assert docs[0].title == "Deep Learning"
    assert docs[0].url == "http://arxiv.org/abs/1234.5678"
