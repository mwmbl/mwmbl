"""The user migration 0049 makes to credit the seed crawl's pages to."""

import json

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

from mwmbl.models import SEED_CRAWLER_USERNAME

User = get_user_model()


@pytest.mark.django_db
def test_the_seed_crawler_exists_and_cannot_sign_in():
    seed_crawler = User.objects.get(username=SEED_CRAWLER_USERNAME)

    assert not seed_crawler.is_active
    assert not seed_crawler.has_usable_password()


@pytest.mark.django_db
def test_the_seed_crawlers_username_cannot_be_registered(client):
    """Even without the user, as before migration 0049 has run: whoever took the name first
    would fail the migration."""
    User.objects.filter(username=SEED_CRAWLER_USERNAME).delete()

    response = client.post(
        "/api/v1/platform/register",
        data=json.dumps({"email": "new@example.com", "password": "correctpassword", "username": SEED_CRAWLER_USERNAME}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert not User.objects.filter(username=SEED_CRAWLER_USERNAME).exists()


def test_allauth_refuses_the_seed_crawlers_username():
    """Settings cannot import SEED_CRAWLER_USERNAME, so they repeat it."""
    assert settings.ACCOUNT_USERNAME_BLACKLIST == [SEED_CRAWLER_USERNAME]
