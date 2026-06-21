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

USER_AGENT = "mwmbl-super-search/0.1 (+https://mwmbl.org)"

RECIPES = load_recipes()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT})


def _assert_usable(docs, expected_title_substr: str):
    assert docs, "recipe returned no results — the site may have changed its format or blocked us"
    assert docs[0].url and docs[0].title, f"top result missing url/title: {docs[0]!r}"
    assert any(expected_title_substr in d.title for d in docs), \
        f"no result title contains {expected_title_substr!r}; titles={[d.title for d in docs]}"


@pytest.mark.parametrize("recipe", RECIPES.values(), ids=lambda r: r.name)
async def test_recipe_live(recipe):
    async with _client() as client:
        docs = await search_with_recipe(client, recipe, recipe.smoke["query"], 5)
    _assert_usable(docs, recipe.smoke["expect_title_contains"])
