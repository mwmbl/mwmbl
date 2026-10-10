"""Tests for seed crawls (mwmbl.indexer.seed_crawl): what gets crawled, what counts as new,
and what is recorded for the user. The endpoint that starts a crawl is tested with the
rest of Combined Search, in test_combined_search.py.
"""

import json
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
    SEED: _page(
        "Tokio runtime",
        ["https://tokio.rs/tutorial", "https://elsewhere.example.com/", "https://tokio.rs/search?q=spawn"],
    ),
    "https://tokio.rs/tutorial": _page("Tokio tutorial", ["https://tokio.rs/tutorial/spawning", SEED]),
    "https://tokio.rs/tutorial/spawning": _page("Spawning tasks"),
    "https://elsewhere.example.com/": _page("Elsewhere"),
    "https://tokio.rs/search?q=spawn": _page("Search results"),
    "https://tokio.rs/gone": _page("404 Not Found", ["https://tokio.rs/tutorial"]),
}
# Pages that answer with an error status rather than 200.
STATUSES = {"https://tokio.rs/gone": 404}


@pytest.fixture
def fake_web(monkeypatch, settings):
    """Serve WEB in place of the network, recording each round's batch."""
    settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS = 0
    rounds = []

    def fake_crawl_batch(urls, num_threads, delay_seconds, redis):
        rounds.append(urls)
        return [
            {"url": url, "status": STATUSES.get(url, 200), "timestamp": 1_700_000_000_000, "content": WEB.get(url)}
            for url in urls
        ]

    monkeypatch.setattr(seed_crawl, "crawl_batch", fake_crawl_batch)
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    monkeypatch.setattr(index_batches, "filter_blacklisted_documents", lambda documents: documents)
    return rounds


@pytest.fixture
def worker(monkeypatch):
    """The worker, minus its connection cleanup, which would close the test's transaction."""
    monkeypatch.setattr(seed_crawl, "close_old_connections", lambda: None)
    return seed_crawl.run_next_seed_crawl


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


def test_each_domain_stops_at_its_page_limit(fake_web, settings):
    settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN = 2

    rounds = _crawl([SEED, "https://rust-lang.github.io/async-book/"], ["tokio.rs", "rust-lang.github.io"])

    fetched = [url for batch in fake_web for url in batch]
    assert fetched == [SEED, "https://rust-lang.github.io/async-book/", "https://tokio.rs/tutorial"]
    assert sum(num_crawled for num_crawled, _ in rounds) == 3


def test_a_failed_fetch_adds_no_document(fake_web):
    rounds = _crawl(["https://tokio.rs/missing"], ["tokio.rs"])

    assert rounds == [(1, [])]


def test_an_error_page_is_neither_indexed_nor_followed(fake_web):
    rounds = _crawl(["https://tokio.rs/gone"], ["tokio.rs"])

    assert rounds == [(1, [])]


def test_links_with_a_query_string_are_not_followed(fake_web):
    _crawl([SEED], ["tokio.rs"])

    assert "https://tokio.rs/search?q=spawn" not in [url for batch in fake_web for url in batch]


# ---------------------------------------------------------------------------
# Starting a crawl
# ---------------------------------------------------------------------------


def _queued_jobs(redis_cache):
    return [json.loads(job) for job in reversed(redis_cache.lrange(seed_crawl.QUEUE_KEY, 0, -1))]


def test_only_staan_results_missing_from_the_index_are_seeds(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    index_pages = [Document("Async book", "https://rust-lang.github.io/async-book/", "")]

    assert (
        seed_crawl.start_seed_crawl(1, "tokio", index_pages, STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED
    )

    [job] = _queued_jobs(redis_cache)
    assert job["seed_urls"] == [SEED]
    # Not retrieved, but Combined Search's write found the index already held it.
    assert job["new_seed_urls"] == []
    # Every Staan domain is crawlable, including those of results the index already had.
    assert job["domains"] == ["rust-lang.github.io", "tokio.rs"]


def test_blacklisted_staan_results_are_neither_seeds_nor_domains(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: {SEED})

    seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set())

    [job] = _queued_jobs(redis_cache)
    assert SEED not in job["seed_urls"]
    assert job["domains"] == ["rust-lang.github.io"]


def test_nothing_to_crawl_starts_nothing(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())

    assert (
        seed_crawl.start_seed_crawl(1, "tokio", STAAN_RESULTS, STAAN_RESULTS, set())
        == seed_crawl.CrawlOutcome.ALREADY_INDEXED
    )
    assert seed_crawl.get_seed_crawl(1, "tokio") is None
    assert _queued_jobs(redis_cache) == []


def test_nothing_starts_when_staan_returned_nothing_crawlable(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: {SEED})

    assert seed_crawl.start_seed_crawl(1, "tokio", [], [STAAN_RESULTS[0]], set()) == seed_crawl.CrawlOutcome.NO_RESULTS
    assert seed_crawl.start_seed_crawl(1, "tokio", [], [], set()) == seed_crawl.CrawlOutcome.NO_RESULTS
    assert _queued_jobs(redis_cache) == []


def test_a_user_has_one_crawl_at_a_time(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())

    assert seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED
    assert seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.ALREADY_RUNNING
    assert seed_crawl.start_seed_crawl(1, "rust", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.ALREADY_RUNNING
    assert seed_crawl.get_active_seed_crawl_query(1) == "tokio"
    assert seed_crawl.start_seed_crawl(2, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED
    assert len(_queued_jobs(redis_cache)) == 2


def test_nothing_is_queued_once_the_queue_is_full(redis_cache, monkeypatch, settings):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    settings.SEED_CRAWL_MAX_QUEUED = 1

    assert seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED
    assert seed_crawl.start_seed_crawl(2, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.QUEUE_FULL
    assert seed_crawl.get_seed_crawl(2, "tokio") is None


# ---------------------------------------------------------------------------
# Running a crawl
# ---------------------------------------------------------------------------


def _run(user, index_path, seed_urls=(SEED,), new_seed_urls=()):
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())
    # Crawl only the seeds and domain given, whatever start_seed_crawl made of STAAN_RESULTS.
    seed_crawl.get_redis_connection("default").delete(seed_crawl.QUEUE_KEY)
    seed_crawl.run_seed_crawl(user.id, "tokio", list(seed_urls), list(new_seed_urls), ["tokio.rs"], index_path)
    return seed_crawl.get_seed_crawl(user.id, "tokio")


@pytest.mark.django_db
def test_the_record_lists_the_new_pages_once_the_crawl_is_done(fake_web, redis_cache, user, index_path):
    record = _run(user, index_path)

    assert record["status"] == "done"
    assert record["finished_at"] is not None
    assert record["pages_crawled"] == 3
    assert record["pages_indexed"] == 3
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
def test_the_worker_runs_a_queued_crawl(fake_web, redis_cache, user, index_path, worker):
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())

    assert worker(index_path) is True

    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "done"
    # The user can start another crawl once theirs has finished.
    assert seed_crawl.start_seed_crawl(user.id, "rust", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


@pytest.mark.django_db
def test_a_failed_crawl_is_recorded_and_not_retried(fake_web, redis_cache, user, index_path, worker, monkeypatch):
    def failing_crawl_batch(urls, num_threads, delay_seconds, redis):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(seed_crawl, "crawl_batch", failing_crawl_batch)
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())

    worker(index_path)

    record = seed_crawl.get_seed_crawl(user.id, "tokio")
    assert record["status"] == "failed"
    assert record["finished_at"] is not None
    assert _queued_jobs(redis_cache) == []
    assert seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


@pytest.mark.django_db
def test_crawling_a_query_again_keeps_its_pages_and_counts_none_twice(fake_web, redis_cache, user, index_path):
    _run(user, index_path)

    record = _run(user, index_path)
    user.refresh_from_db()

    assert record["status"] == "done"
    assert record["pages_indexed"] == 3
    assert record["pages_crawled"] == 6
    assert user.seed_search_pages_indexed == 3


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
    assert record["pages"] == []


def _index_staan_snippet_of_the_seed(index_path):
    """What Combined Search writes before the crawl starts."""
    index_batches.index_documents([Document("Tokio", SEED, "An async runtime.")], index_path)


@pytest.mark.django_db
def test_a_seed_combined_search_found_new_is_counted_once_crawled(fake_web, redis_cache, user, index_path):
    _index_staan_snippet_of_the_seed(index_path)

    record = _run(user, index_path, new_seed_urls=[SEED])

    assert SEED in [page["url"] for page in record["pages"]]


@pytest.mark.django_db
def test_a_seed_the_index_already_held_is_not_counted(fake_web, redis_cache, user, index_path):
    _index_staan_snippet_of_the_seed(index_path)

    record = _run(user, index_path, new_seed_urls=[])

    assert SEED not in [page["url"] for page in record["pages"]]


@pytest.mark.django_db
def test_a_seed_the_blacklist_drops_is_not_counted(fake_web, redis_cache, user, index_path, monkeypatch):
    monkeypatch.setattr(
        index_batches, "filter_blacklisted_documents", lambda documents: [d for d in documents if d.url != SEED]
    )

    record = _run(user, index_path, new_seed_urls=[SEED])

    assert SEED not in [page["url"] for page in record["pages"]]


@pytest.mark.django_db
def test_the_summary_counts_the_pages_without_listing_them(fake_web, redis_cache, user, index_path):
    _run(user, index_path)

    summary = seed_crawl.get_seed_crawl_summary(user.id, "tokio")

    assert summary["pages_indexed"] == 3
    assert "pages" not in summary


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

    assert index_new_documents([first], index_path).new == {first.url}
    indexed = index_new_documents([first, second], index_path)
    assert indexed.stored == {first.url, second.url}
    assert indexed.new == {second.url}


def test_index_new_documents_counts_nothing_the_blacklist_drops(index_path, monkeypatch):
    monkeypatch.setattr(index_batches, "filter_blacklisted_documents", lambda documents: [])

    indexed = index_new_documents([Document("Tokio", SEED, "Tokio.")], index_path)

    assert indexed == (set(), set())
