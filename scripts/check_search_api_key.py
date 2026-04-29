#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "python-dotenv"]
# ///
"""
Check search API behaviour with an API key:
  1. Usage value increases with each successful request
  2. Rate limiting kicks in and returns 429 when exceeded
"""
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.environ.get("MWMBL_BASE_URL", "http://localhost:8000")
API_KEY = os.environ.get("MWMBL_API_KEY", "")
JWT_TOKEN = os.environ.get("MWMBL_JWT_TOKEN", "")

SEARCH_URL = f"{BASE_URL}/api/v1/search/"
SUBSCRIPTION_URL = f"{BASE_URL}/api/v1/platform/billing/subscription"
QUERY = "python"


def search(session: requests.Session) -> requests.Response:
    return session.get(SEARCH_URL, params={"s": QUERY}, headers={"X-API-Key": API_KEY})


def check_usage_increases() -> None:
    print("--- Check: monthly_usage increases ---")
    session = requests.Session()
    prev_usage = None
    for i in range(3):
        resp = search(session)
        if resp.status_code != 200:
            print(f"  Request {i + 1}: unexpected status {resp.status_code} — {resp.text}")
            continue
        data = resp.json()
        print("Response data", data)
        usage = data.get("monthly_usage")
        limit = data.get("monthly_limit")
        print(f"  Request {i + 1}: monthly_usage={usage}, monthly_limit={limit}")
        assert usage is not None, "monthly_usage should not be null when using an API key"
        if prev_usage is not None:
            assert usage > prev_usage, (
                f"monthly_usage did not increase: was {prev_usage}, now {usage}"
            )
        prev_usage = usage
        # Small pause to avoid triggering rate limit between checks
        time.sleep(0.3)
    print("  PASS: usage increases with each request\n")


def check_invalid_key_rejected() -> None:
    print("--- Check: invalid API key returns 401 ---")
    resp = requests.get(SEARCH_URL, params={"s": QUERY}, headers={"X-API-Key": "invalid-key"})
    print(f"  status={resp.status_code}, body={resp.json()}")
    assert resp.status_code == 401, f"Expected 401 for invalid key, got {resp.status_code}"
    print("  PASS: invalid API key returns 401\n")


def check_rate_limit() -> None:
    print("--- Check: rate limiting returns 429 ---")
    session = requests.Session()
    statuses = []
    # Fire 10 requests as fast as possible; the limit is 5/sec so we expect 429s
    for _ in range(10):
        resp = search(session)
        statuses.append(resp.status_code)

    count_200 = statuses.count(200)
    count_429 = statuses.count(429)
    other = [s for s in statuses if s not in (200, 429)]
    print(f"  200 OK: {count_200}, 429 Too Many Requests: {count_429}, other: {other}")

    assert count_429 > 0, "Expected at least one 429 but got none"
    assert not other, f"Unexpected status codes: {other}"
    print("  PASS: rate limit triggers 429\n")

    # Also inspect the 429 response body
    # Re-fire until we get one to check the message
    for _ in range(10):
        resp = search(session)
        if resp.status_code == 429:
            body = resp.json()
            print(f"  429 response body: {body}")
            assert "detail" in body, "429 response should include a 'detail' field"
            print("  PASS: 429 response has 'detail' field\n")
            return
    print("  WARNING: could not capture a 429 response body in the second burst\n")


def check_subscription_usage(expected_usage: int) -> None:
    print("--- Check: subscription endpoint reflects correct usage ---")
    if not JWT_TOKEN:
        print("  SKIP: MWMBL_JWT_TOKEN not set\n")
        return
    resp = requests.get(SUBSCRIPTION_URL, headers={"Authorization": f"Bearer {JWT_TOKEN}"})
    assert resp.status_code == 200, f"Expected 200 from subscription, got {resp.status_code}: {resp.text}"
    data = resp.json()
    usage = data.get("monthly_usage")
    limit = data.get("monthly_limit")
    plan = data.get("plan")
    print(f"  plan={plan}, monthly_usage={usage}, monthly_limit={limit}")
    assert usage is not None, "monthly_usage missing from subscription response"
    assert limit is not None, "monthly_limit missing from subscription response"
    assert usage >= expected_usage, (
        f"subscription monthly_usage ({usage}) is less than search usage ({expected_usage})"
    )
    print(f"  PASS: subscription usage ({usage}) >= search usage ({expected_usage})\n")


def main() -> None:
    if not API_KEY:
        print("Set MWMBL_API_KEY environment variable before running this script.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {BASE_URL}\n")

    try:
        check_invalid_key_rejected()
        check_usage_increases()
        # Capture usage after a single search to compare against subscription
        time.sleep(0.3)
        resp = search(requests.Session())
        last_search_usage = resp.json().get("monthly_usage", 0) if resp.status_code == 200 else 0
        check_rate_limit()
        check_subscription_usage(last_search_usage)
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)

    print("All checks passed.")


if __name__ == "__main__":
    main()
