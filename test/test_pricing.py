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
