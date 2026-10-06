"""Tests for seed crawls (mwmbl.indexer.seed_crawl): what gets crawled, what counts as new,
and what is recorded for the user. The endpoint that starts a crawl is tested with the
rest of Combined Search, in test_combined_search.py.
"""

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.indexer import index_batches, seed_crawl
from mwmbl.indexer.index_batches import index_new_documents
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex

User = get_user_model()

SEED = "https://www.tokio.rs/"
STAAN_RESULTS = [
    Document("Tokio", SEED, "An async runtime.", source=DocumentSource.STAAN),
    Document("Async book", "https://rust-lang.github.io/async-book/", "Async Rust.", source=DocumentSource.STAAN),
]


def _page(title, links=()):
    return {"title": title, "extract": f"About {title}.", "links": list(links), "extra_links": []}


# The web the fake crawler sees: url -> content, or None for a fetch that failed.
WEB = {
    SEED: _page("Tokio runtime", ["https://tokio.rs/tutorial", "https://elsewhere.example.com/"]),
    "https://tokio.rs/tutorial": _page("Tokio tutorial", ["https://tokio.rs/tutorial/spawning", SEED]),
    "https://tokio.rs/tutorial/spawning": _page("Spawning tasks"),
    "https://elsewhere.example.com/": _page("Elsewhere"),
}


@pytest.fixture
def fake_web(monkeypatch, settings):
    """Serve WEB in place of the network, recording each round's batch."""
    settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS = 0
    rounds = []

    def fake_crawl_batch(urls, num_threads, delay_seconds, redis):
        rounds.append(urls)
        return [{"url": url, "timestamp": 1_700_000_000_000, "content": WEB.get(url)} for url in urls]

    monkeypatch.setattr(seed_crawl, "crawl_batch", fake_crawl_batch)
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    monkeypatch.setattr(index_batches, "filter_blacklisted_documents", lambda documents: documents)
    return rounds


@pytest.fixture
def index_path(tmp_path):
    path = Path(tmp_path) / "index.tinysearch"
    with TinyIndex.create(Document, str(path), num_pages=64, page_size=4096):
        pass
    return str(path)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="seeder", email="seed@example.com", password="x")


def _crawl(seed_urls, domains):
    return [round_ for round_ in seed_crawl.crawl_within_domains(seed_urls, set(domains), redis=None)]


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------


def test_links_are_followed_within_the_domains_and_no_further(fake_web):
    rounds = _crawl([SEED], ["tokio.rs"])

    crawled = [document.url for _, documents in rounds for document in documents]
    assert crawled == [SEED, "https://tokio.rs/tutorial", "https://tokio.rs/tutorial/spawning"]


def test_a_page_is_crawled_once_however_often_it_is_linked(fake_web):
    _crawl([SEED], ["tokio.rs"])

    fetched = [url for batch in fake_web for url in batch]
    assert len(fetched) == len(set(fetched))


def test_each_round_fetches_at_most_one_url_per_domain(fake_web):
    _crawl(
        [SEED, "https://tokio.rs/tutorial", "https://rust-lang.github.io/async-book/"],
        ["tokio.rs", "rust-lang.github.io"],
    )

    assert fake_web[0] == [SEED, "https://rust-lang.github.io/async-book/"]
    assert all(len({url.split("/")[2].removeprefix("www.") for url in batch}) == len(batch) for batch in fake_web)


def test_the_crawl_stops_at_the_page_limit(fake_web, settings):
    settings.SEED_CRAWL_MAX_PAGES = 2

    rounds = _crawl([SEED], ["tokio.rs"])

    assert sum(num_crawled for num_crawled, _ in rounds) == 2


def test_a_failed_fetch_adds_no_document(fake_web):
    rounds = _crawl(["https://tokio.rs/missing"], ["tokio.rs"])

    assert rounds == [(1, [])]


# ---------------------------------------------------------------------------
# Starting a crawl
# ---------------------------------------------------------------------------


def test_only_staan_results_missing_from_the_index_are_seeds(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    index_pages = [Document("Async book", "https://rust-lang.github.io/async-book/", "")]

    seed_urls, domains = seed_crawl.start_seed_crawl(1, "tokio", index_pages, STAAN_RESULTS)

    assert seed_urls == [SEED]
    # Every Staan domain is crawlable, including those of results the index already had.
    assert domains == ["rust-lang.github.io", "tokio.rs"]


def test_blacklisted_staan_results_are_neither_seeds_nor_domains(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: {SEED})

    seed_urls, domains = seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS)

    assert SEED not in seed_urls
    assert domains == ["rust-lang.github.io"]


def test_nothing_to_crawl_starts_nothing(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())

    assert seed_crawl.start_seed_crawl(1, "tokio", STAAN_RESULTS, STAAN_RESULTS) is None
    assert seed_crawl.get_seed_crawl(1, "tokio") is None


def test_a_running_crawl_is_not_started_again(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())

    assert seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS) is not None
    assert seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS) is None
    assert seed_crawl.start_seed_crawl(2, "tokio", [], STAAN_RESULTS) is not None


# ---------------------------------------------------------------------------
# Running a crawl
# ---------------------------------------------------------------------------


def _run(user, index_path, seed_urls=(SEED,)):
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS)
    seed_crawl.run_seed_crawl(user.id, "tokio", list(seed_urls), ["tokio.rs"], index_path)
    return seed_crawl.get_seed_crawl(user.id, "tokio")


@pytest.mark.django_db
def test_the_record_lists_the_new_pages_once_the_crawl_is_done(fake_web, redis_cache, user, index_path):
    record = _run(user, index_path)

    assert record["status"] == "done"
    assert record["finished_at"] is not None
    assert record["pages_crawled"] == 3
    assert [page["url"] for page in record["pages"]] == [
        SEED,
        "https://tokio.rs/tutorial",
        "https://tokio.rs/tutorial/spawning",
    ]
    assert record["pages"][1] == {
        "url": "https://tokio.rs/tutorial",
        "title": "Tokio tutorial",
        "extract": "About Tokio tutorial.",
    }


@pytest.mark.django_db
def test_the_crawled_pages_are_in_the_index(fake_web, redis_cache, user, index_path):
    _run(user, index_path)

    with TinyIndex(Document, index_path, "r") as index:
        assert "https://tokio.rs/tutorial/spawning" in {document.url for document in index.retrieve("spawning")}


@pytest.mark.django_db
def test_pages_already_in_the_index_are_not_counted_as_new(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    other = User.objects.create_user(username="other", email="other@example.com", password="x")

    record = _run(other, index_path, seed_urls=["https://tokio.rs/tutorial"])

    assert record["pages_crawled"] == 3
    assert [page["url"] for page in record["pages"]] == ["https://tokio.rs/tutorial"]


@pytest.mark.django_db
def test_new_pages_are_added_to_the_users_total(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    user.refresh_from_db()

    assert user.seed_search_pages_indexed == 3


@pytest.mark.django_db
def test_the_users_stats_report_their_seed_search_total(user):
    User.objects.filter(id=user.id).update(seed_search_pages_indexed=12)
    token = str(RefreshToken.for_user(user).access_token)

    response = Client().get("/api/v1/platform/user/stats", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.json()["seed_search_pages_indexed"] == 12


def test_index_new_documents_returns_only_urls_the_index_lacked(index_path, monkeypatch):
    monkeypatch.setattr(index_batches, "filter_blacklisted_documents", lambda documents: documents)
    first = Document("Tokio tutorial", "https://tokio.rs/tutorial", "Learn tokio.")
    second = Document("Spawning tasks", "https://tokio.rs/spawning", "Spawning with tokio.")

    assert index_new_documents([first], index_path) == {first.url}
    assert index_new_documents([first, second], index_path) == {second.url}
