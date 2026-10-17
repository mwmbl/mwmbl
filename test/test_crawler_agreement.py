from datetime import timedelta
from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from mwmbl.crawler.agreement import AGREEMENT_RETENTION, prune_agreements, record_agreements
from mwmbl.indexer.agreement import Agreement, find_agreements, trigram_similarity
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index_batches import index_documents
from mwmbl.models import ApiKey, CrawlerAgreement, generate_api_key
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex

URL = "https://example.com/page"


def test_trigram_similarity_of_identical_text_is_one():
    assert trigram_similarity("The Python Tutorial", "the  python tutorial") == 1.0


def test_trigram_similarity_of_unrelated_text_is_zero():
    assert trigram_similarity("abcdef", "uvwxyz") == 0.0


def test_trigram_similarity_of_short_text():
    assert trigram_similarity("", "") == 1.0
    assert trigram_similarity("a", "b") == 0.0


def test_trigram_similarity_of_an_edit_is_between():
    similarity = trigram_similarity("Python is a programming language", "Python is a great programming language")
    assert 0.5 < similarity < 1.0


def test_find_agreements_compares_against_another_crawlers_copy():
    existing = [Document("Title", URL, "An extract", term="t", user_ids=[1, 2], last_crawled=1000)]
    incoming = [Document("Title", URL, "Buy cheap pills", term="t", user_ids=[3], last_crawled=1600)]

    (agreement,) = find_agreements(incoming, existing)

    assert agreement.url == URL
    assert agreement.user_id == 3
    assert agreement.existing_user_ids == (1, 2)
    assert agreement.title_similarity == 1.0
    assert agreement.extract_similarity == 0.0
    assert agreement.last_crawled_delta == 600


def test_find_agreements_skips_the_crawlers_own_copy():
    existing = [Document("Title", URL, "Old extract", term="t", user_ids=[1, 3])]
    incoming = [Document("Title", URL, "New extract", term="t", user_ids=[3])]

    assert find_agreements(incoming, existing) == set()


def test_find_agreements_skips_unattributed_documents():
    """Seed crawls and the main index sync write documents with no user_ids: there is no
    crawler to hold them against."""
    attributed = Document("Title", URL, "Extract", term="t", user_ids=[1])
    unattributed = Document("Title", URL, "Extract", term="t")

    assert find_agreements([attributed], [unattributed]) == set()
    assert find_agreements([unattributed], [attributed]) == set()


def test_find_agreements_skips_other_urls():
    existing = [Document("Title", "https://example.com/other", "Extract", term="t", user_ids=[1])]
    incoming = [Document("Title", URL, "Extract", term="t", user_ids=[2])]

    assert find_agreements(incoming, existing) == set()


def test_find_agreements_without_crawl_times_has_no_delta():
    existing = [Document("Title", URL, "Extract", term="t", user_ids=[1])]
    incoming = [Document("Title", URL, "Extract", term="t", user_ids=[2], last_crawled=1000)]

    (agreement,) = find_agreements(incoming, existing)

    assert agreement.last_crawled_delta is None


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


@pytest.fixture
def no_blacklist():
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()), redis_client=fakeredis.FakeRedis())
    with patch("mwmbl.indexer.index_batches.get_snapshot_blacklist", return_value=blacklist):
        yield


def test_index_documents_reports_one_agreement_per_url(index_path, no_blacklist):
    """The URL is written to the page of each of its terms, most of them holding the same
    stored copy, and that is one agreement, not one per term."""
    index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[1])], index_path)

    indexed = index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[2])], index_path)

    assert indexed.agreements == {
        Agreement(
            url=URL,
            user_id=2,
            existing_user_ids=(1,),
            title_similarity=1.0,
            extract_similarity=1.0,
            last_crawled_delta=None,
        )
    }


def test_index_documents_reports_no_agreement_for_a_new_url(index_path, no_blacklist):
    indexed = index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[1])], index_path)

    assert indexed.agreements == set()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="crawler", email="crawler@example.com", password="pw")


def _agreement(url=URL):
    return Agreement(
        url=url,
        user_id=0,
        existing_user_ids=(1, 2),
        title_similarity=0.9,
        extract_similarity=0.4,
        last_crawled_delta=60,
    )


@pytest.mark.django_db
def test_record_agreements(user):
    record_agreements({_agreement()}, user, "1.2.0")

    stored = CrawlerAgreement.objects.get()
    assert stored.url == URL
    assert stored.user == user
    assert stored.existing_user_ids == [1, 2]
    assert stored.title_similarity == 0.9
    assert stored.extract_similarity == 0.4
    assert stored.last_crawled_delta == 60
    assert stored.crawler_version == "1.2.0"


@pytest.mark.django_db
def test_prune_agreements_deletes_only_expired_rows(user):
    record_agreements({_agreement("https://example.com/old"), _agreement("https://example.com/new")}, user, None)
    expired = timezone.now() - AGREEMENT_RETENTION - timedelta(days=1)
    CrawlerAgreement.objects.filter(url="https://example.com/old").update(created_at=expired)

    assert prune_agreements() == 1
    assert list(CrawlerAgreement.objects.values_list("url", flat=True)) == ["https://example.com/new"]


@pytest.mark.django_db
def test_post_results_records_agreements(user, index_path, no_blacklist):
    other_user = get_user_model().objects.create_user(username="other", email="other@example.com", password="pw")
    index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[other_user.id])], index_path)
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])

    with (
        patch("mwmbl.crawler.app.settings.DATA_PATH", str(index_path).rsplit("/", 1)[0]),
        patch("mwmbl.crawler.app.settings.INDEX_NAME", "test.tinysearch"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        response = Client().post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={
                "results": [{"url": URL, "title": "Python tutorial", "extract": "Learn rust today"}],
                "crawler_version": "1.2.0",
            },
            HTTP_X_API_KEY=raw_key,
        )

    assert response.status_code == 200
    stored = CrawlerAgreement.objects.get()
    assert stored.user == user
    assert stored.existing_user_ids == [other_user.id]
    assert stored.title_similarity == 1.0
    assert stored.extract_similarity < 1.0
    assert stored.crawler_version == "1.2.0"
