#!/usr/bin/env python3
"""Pre-screen candidate domains: detect bot-blocking and harvest search URLs.

Stage 0 of recipe authoring. For each domain (read from recipe_chunks batches),
fetch the homepage ONCE with the same httpx client the recipe engine uses, then:

  * classify it as ``blocked`` (Cloudflare / 403 / 429 / challenge page),
    ``error`` (DNS / timeout / connection), or ``ok``;
  * for ``ok`` sites, harvest candidate search-URL templates the site itself
    advertises -- an OpenSearch ``<link>`` descriptor (standardised
    ``template="...{searchTerms}..."``) and any ``<form>`` whose action looks
    like a search -- so a later stage can try the site's OWN search endpoint
    instead of only the formulaic API guesses the brute-forcer already covered.

Blocked sites are given up on immediately (they'd fail the production engine's
client too, so any recipe would fail its smoke gate anyway). The output JSON
feeds the generation stage with only reachable sites + concrete URL candidates.

Skips domains that already have a recipe. Runs entirely in the main thread.

Usage:
  uv run python scripts/prescreen_sites.py --all --out prescreen.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
RECIPES_DIR = REPO_ROOT / "mwmbl" / "tinysearchengine" / "super_search_sources" / "recipes"
CHUNKS_DIR = REPO_ROOT / "recipe_chunks"

ENGINE_HEADERS = {"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"}
TIMEOUT = 15.0
CONCURRENCY = 16

# Substrings that mark a bot-challenge / block page (lowercased body match).
BLOCK_MARKERS = (
    "just a moment", "checking your browser", "attention required",
    "cf-chl", "cf-challenge", "enable javascript and cookies",
    "ddos protection by", "access denied", "request unsuccessful",
    "are you a robot", "captcha-delivery", "px-captcha",
)
BLOCK_STATUSES = {401, 403, 405, 406, 429, 503}
SEARCH_INPUT_NAMES = {"q", "s", "query", "search", "keyword", "keywords", "term", "k"}


def _name(domain: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in domain)


def _classify(status: int, body: str) -> str:
    if status in BLOCK_STATUSES:
        return "blocked"
    low = body[:4000].lower()
    if any(m in low for m in BLOCK_MARKERS):
        return "blocked"
    return "ok"


def _harvest(base: str, html: str) -> dict:
    """Pull OpenSearch descriptor + search-form action(s) from a homepage."""
    soup = BeautifulSoup(html, "html.parser")
    opensearch = None
    for link in soup.find_all("link"):
        rels = {r.lower() for r in (link.get("rel") or [])}
        type_ = (link.get("type") or "").lower()
        if "search" in rels or "opensearchdescription" in type_:
            href = link.get("href")
            if href:
                opensearch = urljoin(base, href)
                break

    forms = []
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        # The real search field is a text/search input. Prefer one whose name is
        # a conventional search param, but fall back to the form's ACTUAL first
        # text-input name -- never a guessed "q" (e.g. Slashdot uses "fhfilter").
        text_names = [
            (i.get("name") or "").strip()
            for i in form.find_all("input")
            if (i.get("name") or "").strip()
            and (i.get("type") or "text").lower() in ("", "text", "search")
        ]
        hit = [n for n in text_names if n.lower() in SEARCH_INPUT_NAMES]
        cls = (form.get("class") or [""])[0]
        looks_search = "search" in action.lower() or "search" in (form.get("id", "") + cls).lower()
        if hit:
            field = hit[0]
        elif looks_search and text_names:
            field = text_names[0]
        else:
            continue
        forms.append({"action": urljoin(base, action or "/"), "param": field})
    # De-dup forms by (action, param).
    seen, uniq = set(), []
    for f in forms:
        key = (f["action"], f["param"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return {"opensearch": opensearch, "forms": uniq[:4]}


async def _screen(client: httpx.AsyncClient, entry: dict) -> dict:
    domain = entry["name"]
    if (RECIPES_DIR / f"{_name(domain)}.yaml").exists():
        return {"domain": domain, "status": "exists"}
    base = f"https://{domain}/"
    try:
        r = await client.get(base)
    except Exception as e:  # noqa: BLE001
        return {"domain": domain, "status": "error", "detail": repr(e)[:120]}
    cls = _classify(r.status_code, r.text)
    out = {"domain": domain, "field": entry.get("field", ""),
           "status": cls, "http": r.status_code}
    if cls == "ok":
        out.update(_harvest(str(r.url), r.text))
    return out


async def _run(entries: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                 headers=ENGINE_HEADERS) as client:
        async def guarded(e):
            async with sem:
                return await _screen(client, e)
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
    ap.add_argument("batches", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default="prescreen.json")
    args = ap.parse_args()

    paths = sorted(str(p) for p in CHUNKS_DIR.glob("input_*.json")) if args.all \
        else args.batches
    if not paths:
        print("no batch files (use --all or list files)", file=sys.stderr)
        return 2

    entries = _load_entries(paths)
    print(f"Screening {len(entries)} domains...")
    results = asyncio.run(_run(entries))

    by = {}
    for r in results:
        by.setdefault(r["status"], []).append(r)
    reachable = by.get("ok", [])
    with_search = [r for r in reachable if r.get("opensearch") or r.get("forms")]
    Path(args.out).write_text(json.dumps(results, indent=2))

    print(f"\n  ok/reachable : {len(reachable)}  "
          f"(of which {len(with_search)} expose an OpenSearch/form search URL)")
    print(f"  blocked      : {len(by.get('blocked', []))}")
    print(f"  error        : {len(by.get('error', []))}")
    print(f"  already exist: {len(by.get('exists', []))}")
    print(f"\nWrote {args.out}. Reachable sites with a discoverable search URL "
          f"are the worthwhile targets for the generation stage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
