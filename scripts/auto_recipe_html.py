#!/usr/bin/env python3
"""Auto-generate HTML-scrape recipes from harvested search URLs.

Companion to auto_recipe.py (which handles formulaic JSON APIs). This consumes
the form/OpenSearch search-URL templates harvested by prescreen_sites.py and,
for each, tries to detect the repeated result container on a real search page
and emit a working HTML recipe -- so the ~115 reachable HTML-scrape sites get
authored deterministically instead of one-by-one.

For each domain it probes the search URL with a few generic queries. On a page
it groups every plausible result link (anchor with href + reasonable text) by
its immediate parent's (tag, class) signature; the most common such group with
>= MIN_RESULTS members is taken to be the result list. It builds a recipe
(results = that container selector, title = the anchor, url = the anchor href)
and validates it in-process with the SAME smoke check as smoke_recipe.py:
>= MIN_RESULTS docs AND the probe word echoed in some title. First passing
(query, container) wins; the YAML is written. Existing recipes are not touched.

JavaScript-only search pages (Sphinx `search.html`, framework SPAs) naturally
fail -- the server returns no result anchors -- so they're skipped automatically.

Input is the worklist JSON written by the caller: a list of
[domain, field, kind, url_template] where url_template contains `{query}`.

Usage:
  DATABASE_URL="postgres://daoud@" uv run python scripts/auto_recipe_html.py /tmp/htmlwork.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")

import django  # noqa: E402
import logging  # noqa: E402

django.setup()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mwmbl.tinysearchengine.super_search_sources.recipe").setLevel(
    logging.ERROR)

import httpx  # noqa: E402
import yaml  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from mwmbl.tinysearchengine.super_search_sources.recipe import (  # noqa: E402
    Recipe, search_with_recipe,
)
from scripts.auto_recipe import FIELD_WORD, GENERIC_WORDS, _name  # noqa: E402
from mwmbl.tinysearchengine.super_search_sources.smoke import (  # noqa: E402
    CONTROL_QUERY, MAX_CONTROL_OVERLAP, control_overlap,
)

RECIPES_DIR = REPO_ROOT / "mwmbl" / "tinysearchengine" / "super_search_sources" / "recipes"
MIN_RESULTS = 3
CONCURRENCY = 10
TIMEOUT = 15.0
# Bare generic tags (no class) match nav/sidebar/footer items indiscriminately;
# only headings or class-bearing selectors reliably isolate real result rows.
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5"}


def _probe_words(field: str) -> list[str]:
    words = [FIELD_WORD[field]] if field in FIELD_WORD else []
    for w in GENERIC_WORDS:
        if w not in words:
            words.append(w)
    return words[:5]


def _class_sig(el) -> str:
    classes = el.get("class") or []
    if classes:
        return el.name + "".join("." + c for c in classes[:2])
    return el.name


def _candidate_selectors(html: str) -> list[str]:
    """Rank result-container selectors by how many titled links they hold."""
    soup = BeautifulSoup(html, "html.parser")
    groups: Counter[str] = Counter()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        if len(a.get_text(" ", strip=True)) < 15:
            continue
        parent = a.parent
        if parent is None:
            continue
        groups[_class_sig(parent)] += 1
    # Keep only selectors that can isolate a result row: a heading tag, or any
    # selector carrying a class. Reject bare generic tags (li/div/p/td/...) and
    # obvious nav/menu chrome.
    ranked = [(sel, n) for sel, n in groups.most_common()
              if n >= MIN_RESULTS
              and ("." in sel or sel in HEADING_TAGS)
              and not any(bad in sel.lower()
                          for bad in ("menu", "nav", "footer", "header"))]
    return [sel for sel, _ in ranked[:3]]


async def _author(client: httpx.AsyncClient, dom: str, field: str, url_tmpl: str) -> dict:
    name = _name(dom)
    if (RECIPES_DIR / f"{name}.yaml").exists():
        return {"domain": dom, "status": "exists"}
    base = f"https://{dom}"
    for word in _probe_words(field):
        try:
            r = await client.get(url_tmpl.replace("{query}", word))
            html = r.text
        except Exception:  # noqa: BLE001
            break  # network dead -> give up on this domain
        for selector in _candidate_selectors(html):
            response = {
                "format": "html", "results": selector, "base_url": base,
                "fields": {"title": "a", "url": {"selector": "a", "attr": "href"}},
            }
            # Reconstruct request the same way the engine will (params from the
            # template's query string) by just pointing url at the templated form.
            request = {"url": url_tmpl.split("?")[0],
                       "params": _params_from(url_tmpl)}
            recipe = Recipe(name=name, request=request, response=response,
                            domain=dom, field=field)
            try:
                docs = await search_with_recipe(client, recipe, word, 10)
            except Exception:  # noqa: BLE001
                docs = []
            if len(docs) < MIN_RESULTS or not any(
                    word in (d.title or "").lower() for d in docs):
                continue
            # Query-invariance guard (same as the hardened smoke gate): reject
            # selectors that return the same URLs for an unrelated control query.
            try:
                control = await search_with_recipe(client, recipe, CONTROL_QUERY, 10)
            except Exception:  # noqa: BLE001
                control = []
            overlap = control_overlap(docs, control)
            if overlap is not None and overlap >= MAX_CONTROL_OVERLAP:
                continue
            doc = {"name": name, "domain": dom, "field": field,
                   "request": request, "response": response,
                   "smoke": {"query": word, "expect_title_contains": word}}
            (RECIPES_DIR / f"{name}.yaml").write_text(
                yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
            return {"domain": dom, "status": "pass",
                    "selector": selector, "word": word, "n": len(docs)}
    return {"domain": dom, "status": "fail"}


def _params_from(url_tmpl: str) -> dict:
    """Turn the query string of a templated URL into a recipe params map."""
    from urllib.parse import urlsplit, parse_qsl
    qs = urlsplit(url_tmpl).query
    params = {}
    for k, v in parse_qsl(qs, keep_blank_values=True):
        # parse_qsl URL-decodes, so the templated value comes back as "{query}".
        params[k] = "{query}" if "{query}" in v else v
    return params


async def _run(work: list[list]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT,
        headers={"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"},
    ) as client:
        async def guarded(w):
            async with sem:
                return await _author(client, w[0], w[1], w[3])
        return await asyncio.gather(*[guarded(w) for w in work])


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: auto_recipe_html.py <worklist.json>", file=sys.stderr)
        return 2
    work = json.loads(Path(sys.argv[1]).read_text())
    # Drop JS-only doc search and off-site search engines up front.
    work = [w for w in work if "search.html" not in w[3] and "google.com" not in w[3]]
    print(f"Authoring HTML recipes for {len(work)} candidates...")
    results = asyncio.run(_run(work))
    passes = [r for r in results if r["status"] == "pass"]
    for r in passes:
        print(f"PASS {r['domain']}  (results={r['selector']!r}, q={r['word']!r}, {r['n']})")
    print(f"\n{len(passes)} passed, "
          f"{sum(1 for r in results if r['status']=='exists')} existed, "
          f"{sum(1 for r in results if r['status']=='fail')} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
