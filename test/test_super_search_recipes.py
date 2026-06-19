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


def test_load_recipes_raises_on_invalid(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: x\nrequest: {}\n")  # no response
    with pytest.raises(KeyError):
        load_recipes(tmp_path)


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
