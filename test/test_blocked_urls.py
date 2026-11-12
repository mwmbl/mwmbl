"""A URL a moderator blocks is dropped by retrieval and indexing, and purged, without blocking its domain."""

import fakeredis
import numpy as np
import pytest
from background_task.models import Task
from django.core.exceptions import ValidationError

from mwmbl.blocked_urls import url_block_key
from mwmbl.imagesearch import commons, seed_images
from mwmbl.indexer import blacklist_snapshot
from mwmbl.indexer.blacklist_providers import CombinedBlacklistProvider, StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist, build_snapshot, publish_snapshot
from mwmbl.indexer.index_batches import filter_blacklisted_documents, index_pages, preprocess_documents
from mwmbl.indexer.purge_blacklisted import purge_documents
from mwmbl.models import BlockedUrl
from mwmbl.signals import BLACKLIST_SNAPSHOT_TASK
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex
from mwmbl.tinysearchengine.rank import find_blacklisted_urls
from test.test_image_search import make_image

FILE_URL = "https://commons.wikimedia.org/wiki/File:Cat.jpg"
OTHER_FILE_URL = "https://commons.wikimedia.org/wiki/File:Dog.jpg"
PURGE_NUM_PAGES = 32
PAGE_URL = "https://example.com/blocked-page"
SAME_DOMAIN_URL = "https://example.com/fine-page"


@pytest.fixture
def snapshot_blacklist(monkeypatch, settings):
    # The test settings have no database; the blocks are read from one.
    settings.HAS_DATABASE = True
    redis_client = fakeredis.FakeRedis()
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()), redis_client=redis_client)
    monkeypatch.setattr(blacklist_snapshot, "_snapshot_blacklist", blacklist)

    def publish():
        publish_snapshot(build_snapshot(CombinedBlacklistProvider([])), redis_client)
        blacklist.load_now()

    return publish


def document(url):
    return Document("Title", url, "Extract", 1.0)


def test_a_commons_file_has_one_key_whatever_the_spelling():
    keys = {
        url_block_key("https://commons.wikimedia.org/wiki/File:Cat.jpg"),
        url_block_key("https://commons.m.wikimedia.org/wiki/File:cat.jpg"),
        url_block_key("https://commons.wikimedia.org/w/index.php?title=Image:Cat.jpg"),
    }
    assert keys == {FILE_URL}


def test_a_title_in_another_unicode_form_is_the_same_file():
    decomposed = "https://commons.wikimedia.org/wiki/File:Cafe\u0301.jpg"
    composed = "https://commons.wikimedia.org/wiki/File:Caf\u00e9.jpg"

    assert url_block_key(decomposed) == url_block_key(composed)


def test_any_other_url_is_its_own_key_without_its_fragment():
    assert url_block_key(PAGE_URL) == PAGE_URL
    assert url_block_key(PAGE_URL + "#section") == PAGE_URL


def test_an_unparseable_url_is_its_own_key_rather_than_an_error():
    assert url_block_key("http://[x/") == "http://[x/"


@pytest.mark.django_db
def test_blocking_a_file_under_a_second_spelling_is_refused_by_the_form_check():
    BlockedUrl.objects.create(url=FILE_URL, reason="abuse")
    again = BlockedUrl(url="https://commons.m.wikimedia.org/wiki/File:cat.jpg", reason="again")

    with pytest.raises(ValidationError):
        again.clean()


def test_a_bare_domain_is_not_a_valid_url_to_block():
    with pytest.raises(ValidationError):
        BlockedUrl(url="example.com", reason="abuse").clean_fields(exclude=["key"])


@pytest.mark.django_db
def test_an_unparseable_url_does_not_break_filtering(snapshot_blacklist):
    BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    snapshot_blacklist()

    assert find_blacklisted_urls([document("http://[x/"), document(PAGE_URL)]) == {PAGE_URL}


@pytest.mark.django_db
def test_saving_a_block_stores_the_key():
    block = BlockedUrl.objects.create(url="https://commons.m.wikimedia.org/wiki/File:cat.jpg", reason="abuse")

    assert block.key == FILE_URL


@pytest.mark.django_db
def test_a_blocked_url_is_found_without_blocking_its_domain(snapshot_blacklist):
    BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    snapshot_blacklist()
    blacklist = blacklist_snapshot.get_snapshot_blacklist()

    assert blacklist.filter_blocked_urls([PAGE_URL, SAME_DOMAIN_URL]) == {PAGE_URL}
    assert blacklist.filter_blacklisted(["example.com"]) == set()


@pytest.mark.django_db
def test_every_spelling_of_a_blocked_commons_file_is_found(snapshot_blacklist):
    BlockedUrl.objects.create(url=FILE_URL, reason="abuse")
    snapshot_blacklist()
    mobile_url = "https://commons.m.wikimedia.org/wiki/File:Cat.jpg"

    blocked = blacklist_snapshot.get_snapshot_blacklist().filter_blocked_urls([mobile_url, OTHER_FILE_URL])

    assert blocked == {mobile_url}


@pytest.mark.django_db
def test_a_url_hash_is_not_mistaken_for_a_domain(snapshot_blacklist):
    BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    snapshot_blacklist()
    array = np.frombuffer(build_snapshot(CombinedBlacklistProvider([])), dtype=blacklist_snapshot.HASH_DTYPE)

    assert len(array) == 1
    assert blacklist_snapshot.get_snapshot_blacklist().filter_blacklisted([PAGE_URL]) == set()


@pytest.mark.django_db
def test_retrieval_drops_a_blocked_url(snapshot_blacklist):
    BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    snapshot_blacklist()

    found = find_blacklisted_urls([document(PAGE_URL), document(SAME_DOMAIN_URL)])

    assert found == {PAGE_URL}


@pytest.mark.django_db
def test_indexing_skips_a_blocked_url(snapshot_blacklist):
    BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    snapshot_blacklist()

    kept = filter_blacklisted_documents([document(PAGE_URL), document(SAME_DOMAIN_URL)])

    assert [kept_document.url for kept_document in kept] == [SAME_DOMAIN_URL]


@pytest.mark.django_db
def test_blocking_and_unblocking_schedule_a_rebuild():
    block = BlockedUrl.objects.create(url=PAGE_URL, reason="abuse")
    assert Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK).count() == 1

    Task.objects.all().delete()
    block.delete()

    assert Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK).count() == 1


def test_purge_removes_a_blocked_url_but_not_its_domain_neighbour(tmp_path):
    index_path = str(tmp_path / "purge.tinysearch")
    TinyIndex.create(item_factory=Document, index_path=index_path, num_pages=PURGE_NUM_PAGES, page_size=PAGE_SIZE)
    blocked = Document("Blocked apples page", PAGE_URL, "apples and oranges", 1.0)
    neighbour = Document("Fine apples page", SAME_DOMAIN_URL, "apples and bananas", 1.0)
    index_pages(index_path, preprocess_documents([blocked, neighbour], index_path))

    with TinyIndex(Document, index_path, "w") as index:
        removed = purge_documents(index, [blocked], lambda domain: False, lambda url: url == PAGE_URL)
    with TinyIndex(Document, index_path, "r") as index:
        remaining = {d.url for page in range(PURGE_NUM_PAGES) for d in index.get_page(page)}

    assert removed == {"example.com": 1}
    assert remaining == {SAME_DOMAIN_URL}


def test_a_snapshot_builds_without_a_database():
    """The standalone crawler publishes its own snapshot, and has no database to read blocks from."""
    array = np.frombuffer(build_snapshot(CombinedBlacklistProvider([])), dtype=blacklist_snapshot.HASH_DTYPE)

    assert len(array) == 0


@pytest.mark.django_db
def test_seed_search_does_not_offer_a_blocked_file(snapshot_blacklist, monkeypatch, settings):
    settings.JEV_API_KEY = "test-key"
    blocked = make_image("Cat.jpg", page_url=FILE_URL)
    fine = make_image("Dog.jpg", page_url=OTHER_FILE_URL)
    monkeypatch.setattr(commons, "search_commons", lambda *args: [blocked, fine])
    BlockedUrl.objects.create(url="https://commons.m.wikimedia.org/wiki/File:cat.jpg", reason="abuse")
    snapshot_blacklist()

    assert seed_images.find_candidates("pets") == [fine]
