#!/usr/bin/env python3
"""Live smoke-test a single Super Search recipe YAML file.

Loads the recipe, runs it against the real site with its `smoke.query`, and
checks that some result's title contains `smoke.expect_title_contains`.

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

from mwmbl.tinysearchengine.super_search_sources.recipe import (  # noqa: E402
    load_recipe,
    search_with_recipe,
)


async def _run(path: str) -> int:
    recipe = load_recipe(path)
    name = recipe.name
    smoke = recipe.smoke or {}
    query = smoke.get("query")
    expect = (smoke.get("expect_title_contains") or "").lower()
    if not query or not expect:
        print(f"FAIL {name}: missing smoke.query / expect_title_contains")
        return 1
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"},
        ) as client:
            docs = await search_with_recipe(client, recipe, query, 10)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {name}: request error {e!r}")
        return 1
    if not docs:
        print(f"FAIL {name}: no results for {query!r}")
        return 1
    if not any(expect in (d.title or "").lower() for d in docs):
        titles = ", ".join((d.title or "")[:40] for d in docs[:3])
        print(f"FAIL {name}: no title contains {expect!r} (got: {titles})")
        return 1
    print(f"PASS {name}: {len(docs)} results")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: smoke_recipe.py <recipe.yaml>", file=sys.stderr)
        return 2
    return asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
