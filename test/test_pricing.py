"""Unit tests for mwmbl.pricing — pure boundary-value checks, no DB/Django needed."""

from mwmbl import pricing


def test_effective_monthly_request_cap_zero_spend_is_free_allowance():
    assert pricing.effective_monthly_request_cap(0) == pricing.FREE_KEYED_MONTHLY_LIMIT


def test_effective_monthly_request_cap_negative_spend_is_free_allowance():
    assert pricing.effective_monthly_request_cap(-100) == pricing.FREE_KEYED_MONTHLY_LIMIT


def test_effective_monthly_request_cap_ten_dollars():
    # $10 = 1000 cents -> 2000 extra requests at $5/1000
    assert pricing.effective_monthly_request_cap(1_000) == 2_000 + 2_000


def test_effective_monthly_request_cap_twenty_five_dollars():
    assert pricing.effective_monthly_request_cap(2_500) == 2_000 + 5_000


def test_effective_monthly_request_cap_hundred_dollars():
    assert pricing.effective_monthly_request_cap(10_000) == 2_000 + 20_000


def test_effective_monthly_request_cap_arbitrary_cents():
    # At $5.00/1000 requests, every cent buys exactly 2 requests (integer ratio).
    assert pricing.effective_monthly_request_cap(750) == 2_000 + 1_500
    assert pricing.effective_monthly_request_cap(751) == 2_000 + 1_502


def test_billable_overage_below_free_allowance_is_zero():
    assert pricing.billable_overage(0) == 0
    assert pricing.billable_overage(pricing.FREE_KEYED_MONTHLY_LIMIT) == 0


def test_billable_overage_above_free_allowance():
    assert pricing.billable_overage(pricing.FREE_KEYED_MONTHLY_LIMIT + 500) == 500


def test_estimated_cost_cents_below_free_allowance_is_zero():
    assert pricing.estimated_cost_cents(pricing.FREE_KEYED_MONTHLY_LIMIT) == 0


def test_estimated_cost_cents_above_free_allowance():
    # 500 overage requests at $5/1000 = 250 cents
    assert pricing.estimated_cost_cents(pricing.FREE_KEYED_MONTHLY_LIMIT + 500) == 250


def test_combined_search_has_no_free_allowance():
    assert pricing.combined_search_monthly_cap(0, 0) == 0


def test_combined_search_cap_from_spend_limit():
    # $10 buys 2,000 Combined Search requests at $5/1000.
    assert pricing.combined_search_monthly_cap(1_000, 0) == 2_000


def test_standard_search_within_the_free_allowance_costs_combined_search_nothing():
    assert pricing.combined_search_monthly_cap(1_000, pricing.FREE_KEYED_MONTHLY_LIMIT) == 2_000


def test_standard_search_overage_comes_out_of_the_combined_search_cap():
    # 1,000 overage requests cost $5, leaving $5 for 1,000 Combined Search requests.
    assert pricing.combined_search_monthly_cap(1_000, pricing.FREE_KEYED_MONTHLY_LIMIT + 1_000) == 1_000


def test_combined_search_spend_comes_out_of_the_standard_search_cap():
    assert pricing.effective_monthly_request_cap(1_000, combined_search_count=1_000) == 2_000 + 1_000


def test_combined_search_spend_never_takes_the_free_allowance():
    assert pricing.effective_monthly_request_cap(1_000, combined_search_count=5_000) == 2_000


def test_the_two_caps_together_never_exceed_the_spend_limit():
    # Odd counts cost fractional cents; each cap rounds the other endpoint's spend up.
    spend_cents = 1_001
    for other_count in (0, 1, 3, 777):
        search_cap = pricing.effective_monthly_request_cap(spend_cents, combined_search_count=other_count)
        assert pricing.estimated_cost_cents(search_cap, other_count) <= spend_cents

        search_count = pricing.FREE_KEYED_MONTHLY_LIMIT + other_count
        combined_search_cap = pricing.combined_search_monthly_cap(spend_cents, search_count)
        assert pricing.estimated_cost_cents(search_count, combined_search_cap) <= spend_cents


def test_estimated_cost_includes_combined_search():
    # 500 overage requests and 500 Combined Search requests, each at $5/1000.
    assert pricing.estimated_cost_cents(pricing.FREE_KEYED_MONTHLY_LIMIT + 500, 500) == 500
