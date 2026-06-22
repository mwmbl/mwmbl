#!/usr/bin/env python3
"""Deterministically auto-generate Super Search recipes for a list of domains.

Instead of dispatching an agent per site (which writes throwaway probe scripts
and needs manual approval, with a near-zero pass rate), this brute-forces the
handful of *formulaic* search endpoints that account for essentially every
recipe we have ever landed:

  1. WordPress REST search   GET /wp-json/wp/v2/search?search=&per_page=
  2. WordPress REST posts     GET /wp-json/wp/v2/posts?search=&per_page=
  3. MediaWiki API            GET /w/api.php?action=query&list=search&srsearch=
                              GET /api.php?...   (fallback path)
  4. Discourse               GET /search.json?q=

For each domain it tries each template with a few generic probe queries. A
template "passes" when it returns >= MIN_RESULTS results AND some probe word is
echoed in a result title -- which makes the written recipe's smoke block
(query = that word, expect_title_contains = that word) self-consistent, so the
output passes scripts/smoke_recipe.py and test_super_search_recipes.py unchanged.

The first passing template wins; its YAML is written to recipes/<name>.yaml.
Existing recipes are never overwritten.

Usage:
  DATABASE_URL="postgres://daoud@" uv run python scripts/auto_recipe.py \
      recipe_chunks/input_000.json recipe_chunks/input_001.json ...
  DATABASE_URL="postgres://daoud@" uv run python scripts/auto_recipe.py --all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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

from mwmbl.tinysearchengine.super_search_sources.recipe import (  # noqa: E402
    Recipe,
    search_with_recipe,
)

RECIPES_DIR = REPO_ROOT / "mwmbl" / "tinysearchengine" / "super_search_sources" / "recipes"
CHUNKS_DIR = REPO_ROOT / "recipe_chunks"

MIN_RESULTS = 3
CONCURRENCY = 16
TIMEOUT = 12.0

# A primary probe word per field (more likely to be echoed in that field's
# result titles), then generic fallbacks. Capped per domain to bound requests.
FIELD_WORD = {
    "academia": "research",
    "art-design": "design",
    "books-literature": "book",
    "business-finance": "business",
    "education": "learning",
    "gaming": "game",
    "history": "history",
    "science": "science",
    "technology": "technology",
    "health": "health",
    "news": "news",
    "music": "music",
    "food": "food",
    "sports": "sport",
    "law-politics": "law",
    "environment": "climate",
    "travel": "travel",
}
GENERIC_WORDS = ["history", "science", "music", "design", "world", "data"]


def _name(domain: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in domain)


def _probe_words(field: str) -> list[str]:
    words = []
    primary = FIELD_WORD.get(field)
    if primary:
        words.append(primary)
    for w in GENERIC_WORDS:
        if w not in words:
            words.append(w)
    return words[:5]


def _templates(domain: str) -> list[tuple[str, dict, dict]]:
    """Return (label, request, response) candidate recipe pieces for a domain."""
    base = f"https://{domain}"
    return [
        (
            "wp-search",
            {"url": f"{base}/wp-json/wp/v2/search",
             "params": {"search": "{query}", "per_page": "{limit}"}},
            {"format": "json", "results": "",
             "fields": {"title": "title", "url": "url"}},
        ),
        (
            "wp-posts",
            {"url": f"{base}/wp-json/wp/v2/posts",
             "params": {"search": "{query}", "per_page": "{limit}"}},
            {"format": "json", "results": "",
             "fields": {"title": "title.rendered", "extract": "excerpt.rendered",
                        "url": "link"},
             "strip_html": ["title", "extract"]},
        ),
        (
            "mediawiki",
            {"url": f"{base}/w/api.php",
             "params": {"action": "query", "list": "search", "format": "json",
                        "srsearch": "{query}", "srlimit": "{limit}"}},
            {"format": "json", "results": "query.search",
             "fields": {"title": "title", "extract": "snippet",
                        "url": {"template": f"{base}/wiki/{{title}}"}},
             "strip_html": ["extract"]},
        ),
        (
            "mediawiki-root",
            {"url": f"{base}/api.php",
             "params": {"action": "query", "list": "search", "format": "json",
                        "srsearch": "{query}", "srlimit": "{limit}"}},
            {"format": "json", "results": "query.search",
             "fields": {"title": "title", "extract": "snippet",
                        "url": {"template": f"{base}/wiki/{{title}}"}},
             "strip_html": ["extract"]},
        ),
        (
            "discourse",
            {"url": f"{base}/search.json", "params": {"q": "{query}"}},
            {"format": "json", "results": "topics",
             "fields": {"title": "title",
                        "url": {"template": f"{base}/t/{{slug}}/{{id}}"}}},
        ),
    ]


async def _probe_domain(client: httpx.AsyncClient, entry: dict) -> dict:
    domain = entry["name"]
    field = entry.get("field", "")
    name = _name(domain)
    if (RECIPES_DIR / f"{name}.yaml").exists():
        return {"domain": domain, "status": "exists"}

    words = _probe_words(field)
    for label, request, response in _templates(domain):
        recipe = Recipe(name=name, request=request, response=response,
                        domain=domain, field=field)
        for word in words:
            try:
                docs = await search_with_recipe(client, recipe, word, 10)
            except Exception:  # noqa: BLE001
                docs = []
            if len(docs) < MIN_RESULTS:
                # Pattern's endpoint clearly absent/broken -> stop trying words.
                if not docs:
                    break
                continue
            if any(word in (d.title or "").lower() for d in docs):
                doc = {
                    "name": name,
                    "domain": domain,
                    "field": field,
                    "request": request,
                    "response": response,
                    "smoke": {"query": word, "expect_title_contains": word},
                }
                (RECIPES_DIR / f"{name}.yaml").write_text(
                    yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
                return {"domain": domain, "status": "pass",
                        "label": label, "word": word, "n": len(docs)}
    return {"domain": domain, "status": "fail"}


async def _run(entries: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT,
        headers={"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"},
    ) as client:
        async def guarded(entry):
            async with sem:
                return await _probe_domain(client, entry)
        return await asyncio.gather(*[guarded(e) for e in entries])


def _load_entries(paths: list[str]) -> list[dict]:
    entries, seen = [], set()
    for p in paths:
        for entry in json.loads(Path(p).read_text()):
            if entry["name"] not in seen:
                seen.add(entry["name"])
                entries.append(entry)
    return entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batches", nargs="*", help="batch JSON files")
    ap.add_argument("--all", action="store_true",
                    help="process every recipe_chunks/input_*.json")
    args = ap.parse_args()

    paths = sorted(str(p) for p in CHUNKS_DIR.glob("input_*.json")) if args.all \
        else args.batches
    if not paths:
        print("no batch files given (use --all or list files)", file=sys.stderr)
        return 2

    entries = _load_entries(paths)
    print(f"Probing {len(entries)} domains from {len(paths)} batch file(s)...")
    results = asyncio.run(_run(entries))

    passes = [r for r in results if r["status"] == "pass"]
    exists = [r for r in results if r["status"] == "exists"]
    for r in passes:
        print(f"PASS {r['domain']}  ({r['label']}, q={r['word']!r}, {r['n']} results)")
    print(f"\n{len(passes)} passed, {len(exists)} already existed, "
          f"{len(results) - len(passes) - len(exists)} failed "
          f"(of {len(results)} domains).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
