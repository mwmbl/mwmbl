"""
Curation and flag timestamps must be timezone-aware: both columns are `DateTimeField`s
and `USE_TZ` is on, so a naive value is stored as if it were local time.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from mwmbl.models import Curation, FlagCuration, MwmblUser


@pytest.fixture
def curator(db):
    return MwmblUser.objects.create_user(username="curator", email="curator@example.com", password="password")


@pytest.mark.django_db
def test_approving_a_result_stores_an_aware_timestamp(client, curator):
    client.force_login(curator)

    response = client.post(
        reverse("approve"),
        {
            "query": "aware timestamps",
            "approve_url": "https://example.com/aware",
            "url": ["https://example.com/aware"],
            "title": ["Aware"],
            "extract": ["An extract"],
            "state": [""],
            "score": ["1.0"],
        },
    )

    assert response.status_code == 200
    curation = Curation.objects.get()
    assert curation.timestamp.tzinfo is not None
    assert curation.timestamp.utcoffset() == timedelta(0)


@pytest.mark.django_db
def test_flagging_a_curation_stores_an_aware_timestamp(client, curator):
    curation = Curation.objects.create(
        user=curator,
        timestamp=timezone.now(),
        query="aware timestamps",
        original_index_results=[],
        original_results=[],
        new_results=[],
        num_changes=1,
    )
    client.force_login(curator)

    response = client.post(
        reverse("flag_curation", args=[curation.id]),
        {"flag": "RELEVANCE", "reason": "Not useful"},
    )

    assert response.status_code == 200
    flag = FlagCuration.objects.get()
    assert flag.timestamp.tzinfo is not None
    assert flag.timestamp.utcoffset() == timedelta(0)
