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

from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipes
from mwmbl.tinysearchengine.super_search_sources.smoke import check_recipe

pytestmark = pytest.mark.live

USER_AGENT = "mwmbl-super-search/0.1 (+https://mwmbl.org)"

RECIPES = load_recipes()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT})


@pytest.mark.parametrize("recipe", RECIPES.values(), ids=lambda r: r.name)
async def test_recipe_live(recipe):
    async with _client() as client:
        ok, reason = await check_recipe(client, recipe)
    assert ok, reason
