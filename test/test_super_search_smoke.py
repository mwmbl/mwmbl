"""Live smoke tests for the Super Search YAML recipes.

Unlike test_super_search_recipes.py (which mocks HTTP), these hit the REAL
external endpoints to catch sites changing their response format or blocking
us. They are marked ``live`` and excluded from the default test run; the
weekly "Super Search smoke" GitHub Actions workflow runs them.

Run locally with:

    uv run pytest -m live test/test_super_search_smoke.py

search_with_recipe swallows transport/parse errors and returns [], so a
blocked or reformatted site surfaces here as an empty result list — which the
assertions below turn into a clear failure.
"""
import httpx
import pytest

from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipes, search_with_recipe

pytestmark = pytest.mark.live

QUERY = "frankenstein"
USER_AGENT = "mwmbl-super-search/0.1 (+https://mwmbl.org)"

RECIPES = load_recipes()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT})


def _assert_usable(docs, expected_url_substr: str):
    assert docs, "recipe returned no results — the site may have changed its format or blocked us"
    top = docs[0]
    assert top.url and expected_url_substr in top.url, f"unexpected result URL: {top.url!r}"
    assert top.title, "top result has an empty title"


async def test_wiktionary_live():
    async with _client() as client:
        docs = await search_with_recipe(client, RECIPES["wiktionary"], QUERY, 5)
    _assert_usable(docs, "en.wiktionary.org/wiki/")


async def test_archive_org_live():
    async with _client() as client:
        docs = await search_with_recipe(client, RECIPES["archive_org"], QUERY, 5)
    _assert_usable(docs, "archive.org/details/")


async def test_gutenberg_live():
    async with _client() as client:
        docs = await search_with_recipe(client, RECIPES["gutenberg"], QUERY, 5)
    _assert_usable(docs, "gutenberg.org/ebooks/")
