"""Pure pricing constants and calculations for the usage-based API pricing model.

No Django/DB/cache imports — safe to import from the search hot path, the
billing endpoints, and the background usage-reporting job without risk of
import cycles.
"""

FREE_KEYED_MONTHLY_LIMIT = 2_000  # free requests/month once an API key is presented
PRICE_PER_1000_QUERIES_CENTS = 500  # $5.00 per 1,000 queries
# Keyed Combined Search is billed from the first request: it costs us on every request (EUSP
# and the LLM judge), so it has no free allowance. The same price as standard search for
# now, but metered separately in Polar so the two can diverge.
COMBINED_SEARCH_PRICE_PER_1000_QUERIES_CENTS = 500

# Preset spend-limit options surfaced in the UI (mirrors Brave's Free/$10/$25/$100 presets).
SPEND_LIMIT_PRESETS_CENTS = [0, 1_000, 2_500, 10_000]


def effective_monthly_request_cap(max_monthly_spend_cents: int, combined_search_count: int = 0) -> int:
    """Total requests/month a keyed user may make before being blocked.

    Equal to the free allowance plus however many additional requests the
    configured spend limit buys at $5.00/1,000 requests, once this month's keyed
    Combined Search spend is taken out of it - the two share the one limit. Uses
    integer arithmetic (`* 1000 // PRICE_PER_1000_QUERIES_CENTS`) to avoid rounding
    drift from fractional-cent-per-request division.
    """
    remaining_cents = max_monthly_spend_cents - _cost_cents_rounded_up(
        combined_search_count, COMBINED_SEARCH_PRICE_PER_1000_QUERIES_CENTS
    )
    if remaining_cents <= 0:
        return FREE_KEYED_MONTHLY_LIMIT
    paid_requests = (remaining_cents * 1000) // PRICE_PER_1000_QUERIES_CENTS
    return FREE_KEYED_MONTHLY_LIMIT + paid_requests


def combined_search_monthly_cap(max_monthly_spend_cents: int, search_count: int) -> int:
    """Keyed Combined Search requests/month the spend limit allows.

    What is left of the spend limit once this month's standard-search overage is taken
    out. Each endpoint rounds the other's spend up, so together they never bill more
    than the limit.
    """
    search_spend_cents = _cost_cents_rounded_up(billable_overage(search_count), PRICE_PER_1000_QUERIES_CENTS)
    remaining_cents = max(0, max_monthly_spend_cents - search_spend_cents)
    return (remaining_cents * 1000) // COMBINED_SEARCH_PRICE_PER_1000_QUERIES_CENTS


def _cost_cents_rounded_up(count: int, price_per_1000_cents: int) -> int:
    return -(-count * price_per_1000_cents // 1000)


def billable_overage(monthly_count: int) -> int:
    """Number of requests in the current month that are billable (beyond the free allowance)."""
    return max(0, monthly_count - FREE_KEYED_MONTHLY_LIMIT)


def estimated_cost_cents(monthly_count: int, combined_search_count: int = 0) -> int:
    """Estimated cost in cents for the current month's usage, rounded down."""
    search_cost = billable_overage(monthly_count) * PRICE_PER_1000_QUERIES_CENTS
    combined_search_cost = combined_search_count * COMBINED_SEARCH_PRICE_PER_1000_QUERIES_CENTS
    return (search_cost + combined_search_cost) // 1000
