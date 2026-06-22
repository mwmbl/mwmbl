"""Shared smoke-check logic for Super Search recipes.

Single source of truth used by both the canonical live test
(``test/test_super_search_smoke.py``) and the dev scripts (``smoke_recipe.py``,
``auto_recipe_html.py``). A recipe passes if, for its ``smoke.query``, it
returns at least one result whose title contains ``smoke.expect_title_contains``
AND its result set is *query-sensitive*: an unrelated control query must return
a substantially different set of URLs. A recipe that's really scraping
nav/boilerplate returns the same links regardless of query, so a high overlap
with the control query's results is a failure.
"""
from __future__ import annotations

import httpx

from mwmbl.tinysearchengine.super_search_sources.recipe import (
    Recipe,
    search_with_recipe,
)

# An unrelated control query used to prove the recipe actually *searches*. A
# genuine search returns (almost) nothing for this gibberish.
CONTROL_QUERY = "vrtkqlzxmn"
# Max allowed overlap (Jaccard of result URLs) between the real query and the
# control query before we call the recipe query-invariant boilerplate.
MAX_CONTROL_OVERLAP = 0.5


def control_overlap(docs, control_docs) -> float | None:
    """Jaccard overlap of result URLs between the real and control queries.

    Returns ``None`` when the control query returned nothing — we can't measure
    overlap, and a genuinely empty control result is the strongest evidence the
    recipe is searching rather than scraping boilerplate.
    """
    a = {d.url for d in docs}
    b = {d.url for d in control_docs}
    if not b:
        return None
    return len(a & b) / len(a | b)


async def check_recipe(
    client: httpx.AsyncClient,
    recipe: Recipe,
    top_n: int = 10,
    control_query: str | None = None,
) -> tuple[bool, str]:
    """Run a recipe's smoke check. Returns ``(ok, reason)``.

    ``reason`` is ``""`` on success, otherwise a human-readable description of
    why the recipe failed. ``search_with_recipe`` swallows transport/parse
    errors and returns ``[]``, so a blocked or reformatted site surfaces here as
    a "no results" failure rather than an exception.
    """
    smoke = recipe.smoke or {}
    query = smoke.get("query")
    expect = (smoke.get("expect_title_contains") or "").lower()
    control = control_query or smoke.get("control_query", CONTROL_QUERY)
    if not query or not expect:
        return False, "missing smoke.query / expect_title_contains"

    docs = await search_with_recipe(client, recipe, query, top_n)
    if not docs:
        return False, f"no results for {query!r}"
    if not docs[0].url or not docs[0].title:
        return False, f"top result missing url/title: {docs[0]!r}"
    if not any(expect in (d.title or "").lower() for d in docs):
        titles = ", ".join((d.title or "")[:40] for d in docs[:3])
        return False, f"no title contains {expect!r} (got: {titles})"

    # Query-invariance check: if an unrelated control query returns essentially
    # the same URLs, the recipe is scraping boilerplate, not searching.
    try:
        control_docs = await search_with_recipe(client, recipe, control, top_n)
    except Exception:  # noqa: BLE001
        control_docs = []
    overlap = control_overlap(docs, control_docs)
    if overlap is not None and overlap >= MAX_CONTROL_OVERLAP:
        return False, (
            f"results barely change for control query {control!r} "
            f"(URL overlap {overlap:.0%}) — likely scraping nav/boilerplate, "
            f"not search results"
        )
    return True, ""
