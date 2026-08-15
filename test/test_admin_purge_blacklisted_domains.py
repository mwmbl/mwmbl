import json
import re
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.index_batches import index_documents
from mwmbl.tinysearchengine.indexer import Document, PAGE_SIZE, TinyIndex

PATCH_TARGET = "mwmbl.admin_views.get_default_blacklist_provider"
User = get_user_model()


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


def _get_all_urls(index_path, num_pages=10):
    urls = []
    with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
        for page_index in range(num_pages):
            urls.extend(doc.url for doc in index.get_page(page_index))
    return urls


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(username="staff", password="x", is_staff=True)


@pytest.fixture
def superuser(db):
    return User.objects.create_user(username="admin", password="x", is_staff=True, is_superuser=True)


@pytest.mark.django_db
def test_preview_shows_matches_without_modifying_index(client, staff_user, index_path):
    documents = [
        Document(title="Fine Art Teens - galleries", url="https://fineartteens.com/x", extract="teen gallery photos"),
        Document(title="Good Example", url="https://example.com/y", extract="a good example page"),
    ]
    index_documents(documents, index_path)

    client.force_login(staff_user)
    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        response = client.post(reverse("admin:purge_blacklisted_domains"), {"queries": "fineartteens"})

    assert response.status_code == 200
    assert b"fineartteens.com" in response.content
    assert "https://fineartteens.com/x" in _get_all_urls(index_path)  # not yet removed


@pytest.mark.django_db
def test_preview_embeds_deduplicated_terms_for_the_add_button(client, staff_user, index_path):
    documents = [
        Document(title="Fine Art Teens - galleries", url="https://fineartteens.com/x", extract="teen gallery photos"),
    ]
    index_documents(documents, index_path)

    client.force_login(staff_user)
    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        response = client.post(reverse("admin:purge_blacklisted_domains"), {"queries": "fineartteens"})

    content = response.content.decode()
    match = re.search(
        r'<script id="all-terms-data" type="application/json">(.*?)</script>', content, re.DOTALL)
    assert match, "expected an embedded all-terms-data JSON script"
    terms = json.loads(match.group(1))

    assert "fineartteens" in terms
    # the seed term itself should appear exactly once, not duplicated between
    # found_via_terms and indexed_under_terms
    assert terms.count("fineartteens") == 1
    assert len(terms) == len(set(terms))


@pytest.mark.django_db
def test_confirm_by_staff_non_superuser_is_rejected(client, staff_user, index_path):
    documents = [Document(title="Fine Art Teens", url="https://fineartteens.com/x", extract="teen gallery")]
    index_documents(documents, index_path)

    client.force_login(staff_user)
    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        response = client.post(reverse("admin:purge_blacklisted_domains"), {"queries": "fineartteens", "confirm": "1"})

    assert response.status_code == 200
    assert "https://fineartteens.com/x" in _get_all_urls(index_path)


@pytest.mark.django_db
def test_confirm_by_superuser_removes_matches(client, superuser, index_path):
    documents = [
        Document(title="Fine Art Teens - galleries", url="https://fineartteens.com/x", extract="teen gallery photos"),
        Document(title="Good Example", url="https://example.com/y", extract="a good example page"),
    ]
    index_documents(documents, index_path)

    client.force_login(superuser)
    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        response = client.post(reverse("admin:purge_blacklisted_domains"), {"queries": "fineartteens", "confirm": "1"})

    assert response.status_code == 200
    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


def test_anonymous_is_redirected_to_login(client):
    response = client.post(reverse("admin:purge_blacklisted_domains"), {"queries": "fineartteens"})
    assert response.status_code in (302, 403)
