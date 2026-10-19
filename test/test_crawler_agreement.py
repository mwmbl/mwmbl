from collections import Counter
from datetime import timedelta
from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from mwmbl.crawler.agreement import AGREEMENT_RETENTION, prune_agreements, record_agreements
from mwmbl.crawler.worst_agreements import SUBMITTER, UNKNOWN_GAP, read_worst_agreements
from mwmbl.indexer.agreement import Agreement, StoredCopy, compare, find_stored_copies, trigram_similarity
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index_batches import _most_held_copies, index_documents
from mwmbl.models import ApiKey, CrawlerAgreement, generate_api_key
from mwmbl.search_callers import window_start
from mwmbl.settings import NUM_EXTRACT_CHARS, NUM_TITLE_CHARS
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentSource, DocumentState, TinyIndex
from mwmbl.utils import utc_today

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


def test_find_stored_copies_finds_another_crawlers_copy():
    existing = [Document("Title", URL, "An extract", term="t", user_ids=[1, 2], last_crawled=1000)]
    incoming = [Document("Title", URL, "Buy cheap pills", term="t", user_ids=[3])]

    assert find_stored_copies(incoming, existing) == [(StoredCopy(URL, 2, "Title", "An extract"), 1000)]


def test_find_stored_copies_skips_the_crawlers_own_copy():
    existing = [Document("Title", URL, "Old extract", term="t", user_ids=[1, 3])]
    incoming = [Document("Title", URL, "New extract", term="t", user_ids=[3])]

    assert find_stored_copies(incoming, existing) == []


def test_find_stored_copies_finds_another_crawlers_text_the_crawler_is_credited_on():
    """The crawler is on the stored copy's user_ids, but the text is the last one's."""
    existing = [Document("Title", URL, "Old extract", term="t", user_ids=[3, 1])]
    incoming = [Document("Title", URL, "New extract", term="t", user_ids=[3])]

    ((stored_copy, _),) = find_stored_copies(incoming, existing)

    assert stored_copy.author_id == 1


def test_find_stored_copies_skips_a_curated_copy():
    """A curated copy always wins its page and takes on the user_ids of the copies it beat,
    so its last id is not its author."""
    existing = [Document("Title", URL, "Curated extract", term="t", state=DocumentState.ORGANIC_APPROVED, user_ids=[1])]
    incoming = [Document("Title", URL, "Extract", term="t", user_ids=[2])]

    assert find_stored_copies(incoming, existing) == []


def test_find_stored_copies_skips_a_copy_of_staans():
    """Like a curated copy, it has no author of its own but takes on the user_ids of the
    copies it beats."""
    existing = [Document("Title", URL, "Staan snippet", term="t", source=DocumentSource.STAAN_INDEXED, user_ids=[1])]
    incoming = [Document("Title", URL, "Extract", term="t", user_ids=[2])]

    assert find_stored_copies(incoming, existing) == []


def test_find_stored_copies_skips_unattributed_documents():
    """There is no crawler to hold an unattributed copy against, nor one to compare it for."""
    attributed = Document("Title", URL, "Extract", term="t", user_ids=[1])
    unattributed = Document("Title", URL, "Extract", term="t")

    assert find_stored_copies([attributed], [unattributed]) == []
    assert find_stored_copies([unattributed], [attributed]) == []


def test_find_stored_copies_skips_other_urls():
    existing = [Document("Title", "https://example.com/other", "Extract", term="t", user_ids=[1])]
    incoming = [Document("Title", URL, "Extract", term="t", user_ids=[2])]

    assert find_stored_copies(incoming, existing) == []


def test_compare():
    incoming = Document("Title", URL, "Buy cheap pills", user_ids=[3], last_crawled=1600)

    agreement = compare(incoming, StoredCopy(URL, 2, "Title", "An extract"), stored_last_crawled=1000)

    assert agreement == Agreement(
        url=URL,
        existing_user_id=2,
        title_similarity=1.0,
        extract_similarity=0.0,
        extract_empty=False,
        existing_extract_empty=False,
        last_crawled_delta=600,
        title="Title",
        extract="Buy cheap pills",
        existing_title="Title",
        existing_extract="An extract",
    )


@pytest.mark.parametrize(
    "incoming_extract, stored_extract, extract_empty, existing_extract_empty",
    [
        ("", "An extract", True, False),
        ("An extract", " ", False, True),
        ("", "", True, True),
    ],
)
def test_compare_records_empty_extracts(incoming_extract, stored_extract, extract_empty, existing_extract_empty):
    incoming = Document("Title", URL, incoming_extract, user_ids=[3])

    agreement = compare(incoming, StoredCopy(URL, 2, "Title", stored_extract), stored_last_crawled=None)

    assert (agreement.extract_empty, agreement.existing_extract_empty) == (extract_empty, existing_extract_empty)


def test_compare_without_crawl_times_has_no_delta():
    incoming = Document("Title", URL, "Extract", user_ids=[2], last_crawled=1000)

    agreement = compare(incoming, StoredCopy(URL, 1, "Title", "Extract"), stored_last_crawled=None)

    assert agreement.last_crawled_delta is None


@pytest.fixture
def index_path(tmp_path):
    """Enough pages that a document's terms land on pages of their own, as they do in the
    real index. On a few pages they collide, and a page can then hold an incoming copy that
    combining a colliding term's documents has not touched, hiding what it does to the rest."""
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=100_000, page_size=PAGE_SIZE)
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

    assert indexed.agreements == [
        Agreement(
            url=URL,
            existing_user_id=1,
            title_similarity=1.0,
            extract_similarity=1.0,
            extract_empty=False,
            existing_extract_empty=False,
            last_crawled_delta=None,
            title="Python tutorial",
            extract="Learn python today",
            existing_title="Python tutorial",
            existing_extract="Learn python today",
        )
    ]


@pytest.mark.parametrize(
    "title, extract",
    [
        ("Python tutorial", "Learn python today"),
        ("Python tutorial", "Buy cheap pills buy cheap pills python python python"),
        ("Python tutorial python python python", "Learn python today"),
    ],
)
def test_index_documents_reports_the_agreement_whichever_copy_wins(index_path, no_blacklist, title, extract):
    """Combining merges the copies' user_ids into whichever one ranks first, so the
    comparison has to see them before that, whether the incoming copy ranks above the stored
    one or not."""
    index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[1])], index_path)

    indexed = index_documents([Document(title, URL, extract, user_ids=[2])], index_path)

    (agreement,) = indexed.agreements
    assert agreement.existing_user_id == 1
    assert agreement.title_similarity == trigram_similarity(title, "Python tutorial")
    assert agreement.extract_similarity == trigram_similarity(extract, "Learn python today")


def test_index_documents_reports_one_agreement_when_stored_copies_differ(index_path, no_blacklist):
    """Two crawlers' versions of a URL have different terms, so the pages it is on hold
    different stored copies. A submission meeting both is still one agreement, against the
    copy most of its pages hold."""
    index_documents([Document("Python tutorial", URL, "Learn python today with examples", user_ids=[1])], index_path)
    index_documents([Document("Python tutorial", URL, "Spam", user_ids=[2])], index_path)

    indexed = index_documents(
        [Document("Python tutorial", URL, "Learn python today with examples", user_ids=[3])], index_path
    )

    (agreement,) = indexed.agreements
    assert agreement.extract_similarity == 1.0


def test_most_held_copies_takes_the_copy_most_pages_hold_for_each_url():
    held_copy = StoredCopy(URL, 1, "Title", "Held extract")
    other_copy = StoredCopy(URL, 2, "Title", "Other extract")
    other_url_copy = StoredCopy("https://example.com/other", 3, "Title", "Extract")

    copies = _most_held_copies(Counter({held_copy: 3, other_copy: 2, other_url_copy: 1}))

    assert copies == [held_copy, other_url_copy]


def test_index_documents_compares_a_repeated_url_once_against_its_last_copy(index_path, no_blacklist):
    """However the copies of a URL a submission repeats score, they make one agreement."""
    index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[1])], index_path)

    indexed = index_documents(
        [
            Document("Python tutorial", URL, "Something else entirely", user_ids=[2]),
            Document("Python tutorial", URL, "Learn python today", user_ids=[2]),
        ],
        index_path,
    )

    (agreement,) = indexed.agreements
    assert agreement.extract_similarity == 1.0


def test_index_documents_reports_no_agreement_for_a_new_url(index_path, no_blacklist):
    indexed = index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[1])], index_path)

    assert indexed.agreements == []


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="crawler", email="crawler@example.com", password="pw")


def _agreement(url=URL):
    return Agreement(
        url=url,
        existing_user_id=2,
        title_similarity=0.9,
        extract_similarity=0.4,
        extract_empty=False,
        existing_extract_empty=True,
        last_crawled_delta=60,
        title="Title",
        extract="Extract",
        existing_title="Title",
        existing_extract="",
    )


@pytest.mark.django_db
def test_record_agreements(user):
    record_agreements([_agreement()], user, "1.2.0")

    stored = CrawlerAgreement.objects.get()
    assert stored.url == URL
    assert stored.user == user
    assert stored.existing_user_id == 2
    assert stored.title_similarity == 0.9
    assert stored.extract_similarity == 0.4
    assert not stored.extract_empty
    assert stored.existing_extract_empty
    assert stored.last_crawled_delta == 60
    assert stored.crawler_version == "1.2.0"


@pytest.mark.django_db
def test_prune_agreements_deletes_only_expired_rows(user):
    record_agreements([_agreement("https://example.com/old"), _agreement("https://example.com/new")], user, None)
    expired = timezone.now() - AGREEMENT_RETENTION - timedelta(days=1)
    CrawlerAgreement.objects.filter(url="https://example.com/old").update(created_at=expired)

    assert prune_agreements() == 1
    assert list(CrawlerAgreement.objects.values_list("url", flat=True)) == ["https://example.com/new"]


@pytest.mark.django_db
def test_post_results_rejects_a_crawl_time_before_the_epoch(user):
    """A crawl time from the distant past made the gap to the next crawler's copy overflow."""
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])

    with patch("mwmbl.crawler.app.index_documents") as index_documents:
        response = Client().post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": [{"url": URL, "title": "t", "extract": "e", "last_crawled": -1_000_000_000}]},
            HTTP_X_API_KEY=raw_key,
        )

    assert response.status_code == 422
    index_documents.assert_not_called()


@pytest.mark.django_db
def test_post_results_truncates_titles_and_extracts(user):
    """To what the crawler truncates them to, so a client that does not cannot store more."""
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])

    with (
        patch("mwmbl.crawler.app.index_documents") as index_documents,
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
        patch("mwmbl.crawler.app.record_agreements"),
        patch("mwmbl.crawler.worst_agreements.record_worst_agreements"),
    ):
        Client().post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": [{"url": URL, "title": "t" * 1000, "extract": "e" * 1_000_000}]},
            HTTP_X_API_KEY=raw_key,
        )

    ((document,), _), _ = index_documents.call_args
    assert document.title == "t" * (NUM_TITLE_CHARS - 1) + "…"
    assert document.extract == "e" * (NUM_EXTRACT_CHARS - 1) + "…"


@pytest.mark.django_db
def test_post_results_records_agreements(user, index_path, no_blacklist):
    other_user = get_user_model().objects.create_user(username="other", email="other@example.com", password="pw")
    index_documents([Document("Python tutorial", URL, "Learn python today", user_ids=[other_user.id])], index_path)
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])
    worst_redis = fakeredis.FakeRedis(decode_responses=True)

    with (
        patch("mwmbl.crawler.app.settings.DATA_PATH", str(index_path).rsplit("/", 1)[0]),
        patch("mwmbl.crawler.app.settings.INDEX_NAME", "test.tinysearch"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
        patch("mwmbl.crawler.worst_agreements.get_redis", return_value=worst_redis),
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
    assert stored.existing_user_id == other_user.id
    assert stored.title_similarity == 1.0
    assert stored.extract_similarity < 1.0
    assert stored.crawler_version == "1.2.0"
    kept = read_worst_agreements(worst_redis, user.id, window_start(utc_today()))
    assert [entry["own"]["extract"] for entry in kept[SUBMITTER][UNKNOWN_GAP.name]] == ["Learn rust today"]
