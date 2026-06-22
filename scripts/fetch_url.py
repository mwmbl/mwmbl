#!/usr/bin/env python3
"""Fetch a URL with the SAME httpx client the Super Search recipe engine uses.

This is the discovery tool for the two-stage recipe authoring flow. WebFetch is
often bot-blocked (403/Cloudflare) even when the real engine (plain httpx with a
simple User-Agent) succeeds, which previously pushed agents into writing
throwaway probe scripts (each one a fresh approval prompt). This is ONE fixed,
GET-only command instead: allowlist it once and the discovery agent can probe
candidate search URLs freely, seeing exactly what the recipe engine will see.

Prints the HTTP status, final URL, content-type, and the (truncated) body. For
JSON it pretty-prints so the response shape is obvious to a recipe author.

Usage:
  uv run python scripts/fetch_url.py "https://example.com/search?q=test"
  uv run python scripts/fetch_url.py "https://example.com/api?q=test" --max-chars 12000
  uv run python scripts/fetch_url.py "https://example.com/search?q=test" --out /tmp/page.html
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

# Mirror scripts/smoke_recipe.py so discovery matches what the engine will do.
ENGINE_HEADERS = {"User-Agent": "mwmbl-super-search-smoke/0.1 (+https://mwmbl.org)"}
TIMEOUT = 15.0


async def _fetch(url: str, max_chars: int, out: str | None) -> int:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=TIMEOUT, headers=ENGINE_HEADERS,
        ) as client:
            r = await client.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"FETCH-ERROR: {e!r}")
        return 1

    ctype = r.headers.get("content-type", "")
    print(f"HTTP {r.status_code}  final-url: {r.url}")
    print(f"content-type: {ctype}")
    print(f"length: {len(r.text)} chars\n---")

    if out:
        # Write the full, untruncated body so a recipe author can read/grep the
        # real markup when picking a CSS selector.
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(r.text)
        print(f"wrote {len(r.text)} chars to {out}")
        return 0

    body = r.text
    if "json" in ctype.lower():
        try:
            body = json.dumps(r.json(), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n... [truncated {len(body) - max_chars} chars]"
    print(body)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--out", help="write full untruncated body to this file")
    args = ap.parse_args()
    return asyncio.run(_fetch(args.url, args.max_chars, args.out))


if __name__ == "__main__":
    sys.exit(main())
