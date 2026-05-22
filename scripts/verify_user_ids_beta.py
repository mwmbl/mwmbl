#!/usr/bin/env python3
"""
Smoke-test for the user_ids / last_crawled feature on beta.

Fetches existing results for a single query term from a local dev server,
re-crawls a small number of those URLs, posts them back to the results
endpoint, then checks that user_ids is populated in the raw search response.

Usage:
    MWMBL_API_KEY=<key> MWMBL_CONTACT_INFO=<your@email.com> \
        uv run python scripts/verify_user_ids_beta.py <term>
"""

import argparse
import sys
import os
import time
import json

import requests

# Must be set before importing retrieve (used in the User-Agent string)
os.environ.setdefault("MWMBL_CONTACT_INFO", "beta-test@mwmbl.org")

from mwmbl.crawler.retrieve import crawl_url, CRAWLER_VERSION

BETA_BASE = "http://localhost:8000"
MAX_URLS = 5


def fetch_raw(term: str) -> list[dict]:
    url = f"{BETA_BASE}/api/v1/search/raw?s={requests.utils.quote(term)}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()["results"]


def main():
    parser = argparse.ArgumentParser(description="Verify user_ids tracking on beta")
    parser.add_argument("term", help="Search term to crawl and verify")
    args = parser.parse_args()

    api_key = os.environ.get("MWMBL_API_KEY", "").strip()
    if not api_key:
        print("ERROR: set MWMBL_API_KEY to a crawl-scoped API key", file=sys.stderr)
        sys.exit(1)

    term = args.term

    print(f"[1/4] Fetching existing raw results for '{term}' from beta...")
    existing = fetch_raw(term)
    print(f"      {len(existing)} results found")

    urls = [r["url"] for r in existing[:MAX_URLS]]
    if not urls:
        print("      No URLs to crawl — try a more common search term.")
        sys.exit(1)

    print(f"\n[2/4] Crawling {len(urls)} URL(s)...")
    results = []
    for url in urls:
        print(f"      {url}")
        raw = crawl_url(url)
        content = raw.get("content")
        if content and not raw.get("error") and content.get("title"):
            last_crawled = int(raw["timestamp"] / 1000)  # ms → seconds
            results.append({
                "url": raw["url"],
                "title": content["title"],
                "extract": content.get("extract", ""),
                "last_crawled": last_crawled,
            })
            print(f"        OK — {content['title'][:70]}")
        else:
            err = (raw.get("error") or {}).get("name", "unknown error")
            print(f"        skipped ({err})")

    if not results:
        print("\nNo successful crawls — cannot post or verify.")
        sys.exit(1)

    print(f"\n[3/4] Posting {len(results)} result(s) to beta...")
    payload = {
        "results": results,
        "crawler_version": CRAWLER_VERSION,
    }
    resp = requests.post(
        f"{BETA_BASE}/api/v1/crawler/results",
        json=payload,
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    print(f"      {resp.status_code}: {resp.text}")
    resp.raise_for_status()

    print("\n[4/4] Verifying user_ids in raw results (waiting 2s for index)...")
    time.sleep(2)
    updated = fetch_raw(term)
    print("Updated page", json.dumps(updated, indent=2))

    crawled_urls = {r["url"] for r in results}
    passed, failed = [], []
    for item in updated:
        if item["url"] not in crawled_urls:
            continue
        if item.get("user_ids"):
            passed.append(item)
        else:
            failed.append(item)

    for item in passed:
        print(f"      PASS  {item['url']}")
        print(f"            user_ids={item['user_ids']}  last_crawled={item['last_crawled']}")
    for item in failed:
        print(f"      FAIL  {item['url']}  (user_ids not set)")

    if passed and not failed:
        print(f"\nAll {len(passed)} crawled URL(s) have user_ids set. ✓")
    elif passed:
        print(f"\nPartial: {len(passed)} passed, {len(failed)} failed.")
        sys.exit(1)
    else:
        print("\nFAIL: no crawled items have user_ids in the index.")
        sys.exit(1)


if __name__ == "__main__":
    main()
