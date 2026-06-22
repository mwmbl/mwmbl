#!/usr/bin/env python3
"""Live smoke-test a single Super Search recipe YAML file.

Thin dev wrapper around the shared check in
``mwmbl.tinysearchengine.super_search_sources.smoke`` — the SAME logic the
canonical live test (``test/test_super_search_smoke.py``) enforces in CI. Loads
the recipe, runs it against the real site with its `smoke.query`, checks that
some result's title contains `smoke.expect_title_contains`, and that the result
set actually changes for an unrelated control query (so nav/boilerplate
scrapers are rejected).

Usage:
  uv run python scripts/smoke_recipe.py path/to/recipe.yaml

Prints "PASS <name>" or "FAIL <name>: <reason>" and exits 0/1 accordingly.
"""
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")

import django  # noqa: E402

django.setup()

import httpx  # noqa: E402

from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipe  # noqa: E402
from mwmbl.tinysearchengine.super_search_sources.smoke import check_recipe  # noqa: E402


async def _run(path: str) -> int:
    recipe = load_recipe(path)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=15.0,
        headers={"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"},
    ) as client:
        ok, reason = await check_recipe(client, recipe)
    if ok:
        print(f"PASS {recipe.name}")
        return 0
    print(f"FAIL {recipe.name}: {reason}")
    return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: smoke_recipe.py <recipe.yaml>", file=sys.stderr)
        return 2
    return asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
