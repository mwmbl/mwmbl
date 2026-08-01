"""Pure pricing constants and calculations for the usage-based API pricing model.

No Django/DB/cache imports — safe to import from the search hot path, the
billing endpoints, and the background usage-reporting job without risk of
import cycles.
"""

FREE_KEYED_MONTHLY_LIMIT = 2_000       # free requests/month once an API key is presented
PRICE_PER_1000_QUERIES_CENTS = 500     # $5.00 per 1,000 queries

# Preset spend-limit options surfaced in the UI (mirrors Brave's Free/$10/$25/$100 presets).
SPEND_LIMIT_PRESETS_CENTS = [0, 1_000, 2_500, 10_000]


def effective_monthly_request_cap(max_monthly_spend_cents: int) -> int:
    """Total requests/month a keyed user may make before being blocked.

    Equal to the free allowance plus however many additional requests the
    configured spend limit buys at $5.00/1,000 requests. Uses integer
    arithmetic (`* 1000 // PRICE_PER_1000_QUERIES_CENTS`) to avoid rounding
    drift from fractional-cent-per-request division.
    """
    if max_monthly_spend_cents <= 0:
        return FREE_KEYED_MONTHLY_LIMIT
    paid_requests = (max_monthly_spend_cents * 1000) // PRICE_PER_1000_QUERIES_CENTS
    return FREE_KEYED_MONTHLY_LIMIT + paid_requests


def billable_overage(monthly_count: int) -> int:
    """Number of requests in the current month that are billable (beyond the free allowance)."""
    return max(0, monthly_count - FREE_KEYED_MONTHLY_LIMIT)


def estimated_cost_cents(monthly_count: int) -> int:
    """Estimated cost in cents for the current month's usage, rounded down."""
    return (billable_overage(monthly_count) * PRICE_PER_1000_QUERIES_CENTS) // 1000
