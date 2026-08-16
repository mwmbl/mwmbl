"""Tests for the approved-domain lookup that overrides the blacklists."""
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from mwmbl.curated_domains import CURATED_DOMAINS_CACHE_KEY, get_curated_domains
from mwmbl.models import DomainSubmission, MwmblUser


@pytest.fixture(autouse=True)
def clear_curated_cache():
    cache.delete(CURATED_DOMAINS_CACHE_KEY)
    yield
    cache.delete(CURATED_DOMAINS_CACHE_KEY)


def test_no_database_returns_empty_without_querying():
    """The standalone crawler runs under settings_crawler, which has no database.

    It must not be possible for a blacklist construction there to reach for DomainSubmission
    - the query would raise, and the crawler fetches the same names over HTTP instead.
    """
    with patch("mwmbl.curated_domains.DomainSubmission.objects") as objects:
        with override_settings(HAS_DATABASE=False):
            assert get_curated_domains() == set()
    objects.filter.assert_not_called()


@pytest.mark.django_db
@override_settings(HAS_DATABASE=True)
def test_returns_approved_submissions_only():
    user = MwmblUser.objects.create(username="curator")
    DomainSubmission.objects.create(name="pudding.cool", status="APPROVED", submitted_by=user)
    DomainSubmission.objects.create(name="pending.example", status="PENDING", submitted_by=user)
    DomainSubmission.objects.create(name="rejected.example", status="REJECTED", submitted_by=user)

    assert get_curated_domains() == {"pudding.cool"}


@pytest.mark.django_db
@override_settings(HAS_DATABASE=True)
def test_result_is_cached():
    user = MwmblUser.objects.create(username="curator")
    DomainSubmission.objects.create(name="pudding.cool", status="APPROVED", submitted_by=user)
    assert get_curated_domains() == {"pudding.cool"}

    DomainSubmission.objects.create(name="later.example", status="APPROVED", submitted_by=user)
    assert get_curated_domains() == {"pudding.cool"}

    cache.delete(CURATED_DOMAINS_CACHE_KEY)
    assert get_curated_domains() == {"pudding.cool", "later.example"}
