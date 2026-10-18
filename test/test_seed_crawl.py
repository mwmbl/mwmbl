"""Tests for seed crawls (mwmbl.indexer.seed_crawl): what gets crawled, what counts as new,
and what is recorded for the user. The endpoint that starts a crawl is tested with the
rest of Combined Search, in test_combined_search.py.
"""

import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.indexer import index_batches, seed_crawl, seed_domains
from mwmbl.indexer.index_batches import index_new_documents
from mwmbl.models import SEED_CRAWLER_USERNAME, ApiKey, SeedDomain, SeedDomainCrawl, generate_api_key
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex

User = get_user_model()

SEED = "https://www.tokio.rs/"
# For the rounds run without the worker, which looks the seed crawler's user up itself.
SEED_CRAWLER_ID = 0
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


@pytest.fixture(autouse=True)
def no_connection_cleanup(monkeypatch):
    """The worker's connection cleanup would close the test's transaction."""
    monkeypatch.setattr(seed_crawl, "close_old_connections", lambda: None)


def _run_rounds(index_path):
    """Run the worker until no crawl is due, returning whether any was.

    The fake web has no delay between a domain's fetches, so a crawl is due again as soon as
    its round is done.
    """
    ran = False
    while seed_crawl.run_next_seed_crawl_round(index_path):
        ran = True
    return ran


@pytest.fixture
def worker():
    return _run_rounds


@pytest.fixture
def index_path(tmp_path):
    path = Path(tmp_path) / "index.tinysearch"
    with TinyIndex.create(Document, str(path), num_pages=64, page_size=4096):
        pass
    return str(path)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="seeder", email="seed@example.com", password="x")


def _crawl(seed_urls, domains, is_new=lambda url: True):
    """Run a crawl's rounds to the end, telling it that the pages is_new picks were new."""
    frontier = seed_crawl.Frontier.from_seeds(seed_urls, set(domains))
    crawled = []
    while batch := frontier.next_batch():
        documents = seed_crawl.crawl_round(frontier, batch, redis=None, seed_crawler_id=SEED_CRAWLER_ID)
        crawled.append((len(batch), documents))
        frontier.record_round(batch, {document.url for document in documents if is_new(document.url)})
    return crawled


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


def test_a_domain_is_dropped_once_its_fetches_stop_finding_new_pages(fake_web, monkeypatch, settings):
    settings.SEED_CRAWL_MAX_STALE_PAGES_PER_DOMAIN = 2
    # An endless site: every page links to the next.
    monkeypatch.setitem(WEB, SEED, _page("Page 0", ["https://tokio.rs/1"]))
    for n in range(1, 20):
        monkeypatch.setitem(WEB, f"https://tokio.rs/{n}", _page(f"Page {n}", [f"https://tokio.rs/{n + 1}"]))

    _crawl([SEED], ["tokio.rs"], is_new=lambda url: False)

    fetched = [url for batch in fake_web for url in batch]
    assert fetched == [SEED, "https://tokio.rs/1"]


def test_a_new_page_resets_a_domains_run_of_stale_fetches(fake_web, monkeypatch, settings):
    settings.SEED_CRAWL_MAX_STALE_PAGES_PER_DOMAIN = 2
    monkeypatch.setitem(WEB, SEED, _page("Page 0", ["https://tokio.rs/1"]))
    for n in range(1, 20):
        monkeypatch.setitem(WEB, f"https://tokio.rs/{n}", _page(f"Page {n}", [f"https://tokio.rs/{n + 1}"]))

    _crawl([SEED], ["tokio.rs"], is_new=lambda url: url == "https://tokio.rs/1")

    fetched = [url for batch in fake_web for url in batch]
    assert fetched == [SEED, "https://tokio.rs/1", "https://tokio.rs/2", "https://tokio.rs/3"]


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
    """The crawls queued or running, oldest first, with their seeds as yet unfetched."""
    jobs = []
    for record_key in redis_cache.zrange(seed_crawl.READY_KEY, 0, -1):
        record = {key.decode(): value.decode() for key, value in redis_cache.hgetall(record_key).items()}
        frontier = seed_crawl.Frontier.from_json(record["frontier"])
        jobs.append(
            {
                "seed_urls": [url for queue in frontier.queues.values() for url in queue],
                "new_seed_urls": json.loads(record["new_seed_urls"]),
                "domains": json.loads(record["domains"]),
            }
        )
    return jobs


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
    assert job["domains"] == {"rust-lang.github.io": 1, "tokio.rs": 1}


def test_blacklisted_staan_results_are_neither_seeds_nor_domains(redis_cache, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: {SEED})

    seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set())

    [job] = _queued_jobs(redis_cache)
    assert SEED not in job["seed_urls"]
    assert job["domains"] == {"rust-lang.github.io": 1}


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


@pytest.mark.django_db
def test_a_query_already_crawled_is_not_crawled_again(fake_web, redis_cache, user, index_path, worker):
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())
    worker(index_path)

    outcome = seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())

    assert outcome == seed_crawl.CrawlOutcome.ALREADY_CRAWLED
    assert _queued_jobs(redis_cache) == []
    assert seed_crawl.start_seed_crawl(user.id, "rust", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


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
    """Crawl from the seeds within tokio.rs to the end, as the user's crawl of "tokio"."""
    outcome = seed_crawl.queue_seed_crawl(user.id, "tokio", list(seed_urls), list(new_seed_urls), {"tokio.rs": 1})
    assert outcome == seed_crawl.CrawlOutcome.SCHEDULED
    _run_rounds(index_path)
    return seed_crawl.get_seed_crawl(user.id, "tokio")


def _mark_failed(user, query="tokio"):
    """Mark the user's crawl of the query failed, the one case in which it is crawled again."""
    seed_crawl.get_redis_connection("default").hset(seed_crawl._record_key(user.id, query), "status", "failed")


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
    _mark_failed(user)

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
def test_the_crawled_pages_are_credited_to_the_seed_crawler(fake_web, redis_cache, user, index_path):
    """Not to the user who asked for the crawl: the pages were fetched here, not by them."""
    seed_crawler = User.objects.get(username=SEED_CRAWLER_USERNAME)

    _run(user, index_path)

    with TinyIndex(Document, index_path, "r") as index:
        (spawning,) = [d for d in index.retrieve("spawning") if d.url == "https://tokio.rs/tutorial/spawning"]
    assert spawning.user_ids == [seed_crawler.id]
    assert not seed_crawler.is_active
    assert not seed_crawler.has_usable_password()


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
def test_the_users_total_matches_the_pages_the_crawl_lists(fake_web, redis_cache, user, index_path):
    _index_staan_snippet_of_the_seed(index_path)
    record = _run(user, index_path, new_seed_urls=[SEED])
    user.refresh_from_db()

    assert SEED in [page["url"] for page in record["pages"]]
    assert user.seed_search_pages_indexed == len(record["pages"])


@pytest.mark.django_db
def test_recording_a_seed_search_adds_to_the_users_count(user):
    seed_crawl.record_seed_search(user.id)
    seed_crawl.record_seed_search(user.id)
    user.refresh_from_db()

    assert user.seed_searches == 2


@pytest.mark.django_db
def test_the_users_stats_report_their_seed_search_totals(user):
    User.objects.filter(id=user.id).update(seed_search_pages_indexed=12, seed_searches=4)
    SeedDomain.objects.create(domain="tokio.rs", discovered_by=user)
    SeedDomain.objects.create(domain="smol.rs", discovered_by=user)
    SeedDomain.objects.create(domain="example.com")
    token = str(RefreshToken.for_user(user).access_token)

    response = Client().get("/api/v1/platform/user/stats", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.json()["seed_search_pages_indexed"] == 12
    assert response.json()["seed_searches"] == 4
    assert response.json()["seed_sites_discovered"] == 2


# ---------------------------------------------------------------------------
# Progress and seed domains
# ---------------------------------------------------------------------------


def test_a_queued_crawl_has_no_progress_and_no_domains_yet(redis_cache, monkeypatch, db):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    seed_crawl.start_seed_crawl(1, "tokio", [], STAAN_RESULTS, set())

    summary = seed_crawl.get_seed_crawl_summary(1, "tokio")

    assert summary["progress"] == 0.0
    assert summary["domains"] == []


@pytest.mark.django_db
def test_progress_counts_rounds_against_a_domains_page_limit(redis_cache, user, settings):
    settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN = 4
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())
    record_key = seed_crawl._record_key(user.id, "tokio")
    redis_cache.hset(record_key, mapping={"status": "crawling", "crawl_started_at": seed_crawl._now(), "rounds": 1})

    assert seed_crawl.get_seed_crawl_summary(user.id, "tokio")["progress"] == pytest.approx(0.25, abs=0.01)


@pytest.mark.django_db
def test_a_finished_crawl_reports_its_domains_with_their_scores(fake_web, redis_cache, user, index_path):
    record = _run(user, index_path)

    assert record["progress"] == 1.0
    assert record["domains"] == [
        {
            "domain": "tokio.rs",
            "newly_discovered": True,
            "pages_indexed": 3,
            "new_page_score": 0.15,
            "recent_new_page_score": 0.15,
            "staan_results": 1,
            "score": 0.15,
        }
    ]


@pytest.mark.django_db
def test_the_user_who_first_crawls_a_domain_is_its_discoverer(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    other = User.objects.create_user(username="other", email="other@example.com", password="x")

    record = _run(other, index_path)

    [domain] = record["domains"]
    assert domain["newly_discovered"] is False
    assert domain["staan_results"] == 2
    assert SeedDomain.objects.get(domain="tokio.rs").discovered_by == user


@pytest.mark.django_db
def test_a_domain_the_crawls_have_exhausted_loses_its_score(fake_web, redis_cache, user, index_path, settings):
    settings.SEED_DOMAIN_RECENT_CRAWLS = 2
    _run(user, index_path)
    assert SeedDomain.objects.get(domain="tokio.rs").new_page_score == 0.15

    # The pages are no longer new, so these crawls add none.
    _mark_failed(user)
    _run(user, index_path)
    assert SeedDomain.objects.get(domain="tokio.rs").new_page_score == 0.075
    _mark_failed(user)
    _run(user, index_path)
    seed_domain = SeedDomain.objects.get(domain="tokio.rs")
    assert seed_domain.new_page_score == 0.0
    assert seed_domain.score == 0.0


@pytest.mark.django_db
def test_a_failed_crawl_does_not_score_its_domains(fake_web, redis_cache, user, index_path, monkeypatch):
    def failing_crawl_batch(urls, num_threads, delay_seconds, redis):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(seed_crawl, "crawl_batch", failing_crawl_batch)

    _run(user, index_path)

    assert SeedDomain.objects.get(domain="tokio.rs").staan_results == 1
    assert not SeedDomainCrawl.objects.exists()


@pytest.mark.django_db
def test_progress_by_time_is_capped_at_one(redis_cache, user):
    seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set())
    long_ago = "2020-01-01T00:00:00+00:00"
    redis_cache.hset(
        seed_crawl._record_key(user.id, "tokio"), mapping={"status": "crawling", "crawl_started_at": long_ago}
    )

    assert seed_crawl.get_seed_crawl_summary(user.id, "tokio")["progress"] == 1.0


@pytest.mark.django_db
def test_domains_are_not_reported_until_the_crawl_has_registered_them(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    # A crawl of a fresh query, picked up by the worker but not yet registered.
    seed_crawl.start_seed_crawl(user.id, "rust", [], STAAN_RESULTS, set())
    redis_cache.hset(
        seed_crawl._record_key(user.id, "rust"), mapping={"status": "crawling", "crawl_started_at": seed_crawl._now()}
    )

    assert seed_crawl.get_seed_crawl_summary(user.id, "rust")["domains"] == []


@pytest.mark.django_db
def test_crawling_a_query_again_reports_only_the_new_crawls_domains(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    _mark_failed(user)

    record = _run(user, index_path)

    [domain] = record["domains"]
    assert domain["newly_discovered"] is False
    assert domain["pages_indexed"] == 0
    assert domain["recent_new_page_score"] == 0.075


@pytest.mark.django_db
def test_a_querys_staan_results_count_once_per_user(fake_web, redis_cache, user, index_path, worker):
    tokio_results = [*STAAN_RESULTS, Document("Tokio docs", "https://tokio.rs/docs", "", source=DocumentSource.STAAN)]

    for _ in range(2):
        seed_crawl.start_seed_crawl(user.id, "tokio", [], tokio_results, set())
        worker(index_path)
        _mark_failed(user)

    assert SeedDomain.objects.get(domain="tokio.rs").staan_results == 2
    assert SeedDomain.objects.get(domain="rust-lang.github.io").staan_results == 1


@pytest.mark.django_db
def test_new_pages_reports_progress_and_domains(fake_web, redis_cache, user, index_path):
    _run(user, index_path)
    token = str(RefreshToken.for_user(user).access_token)

    response = Client().get("/api/v2/combined-search/new-pages/count?q=tokio", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 200
    assert response.json()["progress"] == 1.0
    assert [domain["domain"] for domain in response.json()["domains"]] == ["tokio.rs"]
    assert "discovered_by" not in response.json()["domains"][0]


SEED_DOMAINS_URL = "/api/v2/combined-search/seed-domains"


def _api_key(user, scope):
    raw, hashed = generate_api_key()
    ApiKey.objects.create(user=user, key=hashed, name="seed-domains", scopes=[scope])
    return raw


def _seed_domains(user, query=""):
    return Client().get(f"{SEED_DOMAINS_URL}{query}", HTTP_X_API_KEY=_api_key(user, ApiKey.Scope.CRAWL))


@pytest.mark.django_db
def test_the_seed_domains_endpoint_lists_the_best_first_without_their_discoverers(redis_cache, user):
    SeedDomain.objects.create(
        domain="low.example.com", discovered_by=user, new_page_score=0.1, staan_results=1, score=0.1
    )
    SeedDomain.objects.create(
        domain="high.example.com", discovered_by=user, new_page_score=0.5, staan_results=4, score=2
    )

    response = _seed_domains(user, "?limit=1")

    assert response.status_code == 200
    assert response.json() == [{"domain": "high.example.com", "new_page_score": 0.5, "staan_results": 4, "score": 2.0}]


@pytest.mark.django_db
def test_the_seed_domains_endpoint_requires_a_crawl_scoped_key(redis_cache, user):
    search_key = _api_key(user, ApiKey.Scope.SEARCH)

    assert Client().get(SEED_DOMAINS_URL).status_code == 401
    assert Client().get(SEED_DOMAINS_URL, HTTP_X_API_KEY=search_key).status_code == 401


@pytest.mark.django_db
def test_seed_domains_are_published_in_batches(redis_cache, user):
    SeedDomain.objects.create(domain="old.example.com", new_page_score=0.1, staan_results=1, score=0.1)
    assert [domain["domain"] for domain in _seed_domains(user).json()] == ["old.example.com"]

    # What a crawl changes is not shown until the list is next published.
    SeedDomain.objects.create(domain="new.example.com", new_page_score=0.5, staan_results=1, score=0.5)
    SeedDomain.objects.filter(domain="old.example.com").update(score=0.0)
    assert _seed_domains(user).json() == [
        {"domain": "old.example.com", "new_page_score": 0.1, "staan_results": 1, "score": 0.1}
    ]

    cache.delete(seed_domains.PUBLISHED_KEY)
    assert [domain["domain"] for domain in _seed_domains(user).json()] == ["new.example.com", "old.example.com"]


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


# ---------------------------------------------------------------------------
# Workers that die, stall or take turns
# ---------------------------------------------------------------------------


def _claim(redis_cache):
    """Claim the crawl due soonest as a worker would, but run none of it."""
    return redis_cache.eval(
        seed_crawl._CLAIM_SCRIPT,
        1,
        seed_crawl.READY_KEY,
        seed_crawl.time.time(),
        seed_crawl.settings.SEED_CRAWL_LEASE_SECONDS,
        seed_crawl.settings.SEED_CRAWL_MAX_ATTEMPTS,
        seed_crawl._now(),
    )


@pytest.mark.django_db
def test_a_crawl_whose_worker_died_is_resumed_once_its_lease_runs_out(
    fake_web, redis_cache, user, index_path, worker, settings
):
    settings.SEED_CRAWL_LEASE_SECONDS = 0
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    # A worker claims the crawl and dies before writing anything.
    _claim(redis_cache)

    worker(index_path)

    record = seed_crawl.get_seed_crawl(user.id, "tokio")
    assert record["status"] == "done"
    assert record["pages_indexed"] == 3


@pytest.mark.django_db
def test_a_crawl_is_not_claimed_again_while_its_lease_lasts(fake_web, redis_cache, user, index_path, worker):
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    _claim(redis_cache)

    assert worker(index_path) is False
    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "crawling"


@pytest.mark.django_db
def test_a_worker_that_lost_its_claim_writes_nothing(redis_cache, user, settings):
    settings.SEED_CRAWL_LEASE_SECONDS = 0
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    record_key, stale_token = _claim(redis_cache)
    # Too slow: another worker has claimed the crawl since.
    _claim(redis_cache)

    seed_crawl._finish(redis_cache, record_key.decode(), stale_token, user.id, "failed")

    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "crawling"
    assert seed_crawl.get_active_seed_crawl_query(user.id) == "tokio"


@pytest.mark.django_db
def test_a_round_that_keeps_killing_its_workers_fails_the_crawl(redis_cache, user, settings, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    settings.SEED_CRAWL_LEASE_SECONDS = 0
    settings.SEED_CRAWL_MAX_ATTEMPTS = 2
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})

    assert _claim(redis_cache) is not None
    assert _claim(redis_cache) is not None
    assert _claim(redis_cache) is None

    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "failed"
    assert redis_cache.zcard(seed_crawl.READY_KEY) == 0
    assert seed_crawl.start_seed_crawl(user.id, "rust", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


@pytest.mark.django_db
def test_queued_crawls_run_one_after_another_oldest_first(fake_web, redis_cache, user, index_path, worker):
    other = User.objects.create_user(username="other", email="other@example.com", password="x")
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    seed_crawl.queue_seed_crawl(other.id, "tokio", ["https://tokio.rs/tutorial/spawning"], [], {"tokio.rs": 1})

    worker(index_path)

    # The first crawl to the end, then the second.
    assert fake_web == [
        [SEED],
        ["https://tokio.rs/tutorial"],
        ["https://tokio.rs/tutorial/spawning"],
        ["https://tokio.rs/tutorial/spawning"],
    ]
    assert seed_crawl.get_seed_crawl(other.id, "tokio")["status"] == "done"


@pytest.mark.django_db
def test_a_crawls_rounds_are_spaced_by_the_domain_delay(fake_web, redis_cache, user, index_path, settings, monkeypatch):
    settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS = 60
    sleeps = []
    monkeypatch.setattr(seed_crawl.time, "sleep", sleeps.append)
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})

    seed_crawl.run_next_seed_crawl_round(index_path)
    seed_crawl.run_next_seed_crawl_round(index_path)

    assert sleeps == [pytest.approx(60, abs=1)]


@pytest.mark.django_db
def test_a_crawl_left_running_before_crawls_were_leased_is_reported_failed_and_can_be_redone(
    redis_cache, user, monkeypatch
):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    redis_cache.hset(
        seed_crawl._record_key(user.id, "tokio"),
        mapping={"status": "crawling", "query": "tokio", "started_at": seed_crawl._now(), "pages_crawled": 4},
    )

    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "failed"
    assert seed_crawl.start_seed_crawl(user.id, "tokio", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


@pytest.mark.django_db
def test_a_crawl_whose_record_has_expired_is_dropped_and_the_next_one_claimed(
    fake_web, redis_cache, user, index_path, worker
):
    other = User.objects.create_user(username="other", email="other@example.com", password="x")
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    seed_crawl.queue_seed_crawl(other.id, "tokio", [SEED], [], {"tokio.rs": 1})
    redis_cache.delete(seed_crawl._record_key(user.id, "tokio"))

    worker(index_path)

    assert redis_cache.zcard(seed_crawl.READY_KEY) == 0
    assert not redis_cache.exists(seed_crawl._record_key(user.id, "tokio"))
    assert seed_crawl.get_seed_crawl(other.id, "tokio")["status"] == "done"


@pytest.mark.django_db
def test_an_active_crawl_whose_record_has_expired_blocks_nothing(redis_cache, user, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    redis_cache.delete(seed_crawl._record_key(user.id, "tokio"))
    redis_cache.delete(seed_crawl.READY_KEY)

    assert seed_crawl.get_active_seed_crawl_query(user.id) is None
    assert seed_crawl.start_seed_crawl(user.id, "rust", [], STAAN_RESULTS, set()) == seed_crawl.CrawlOutcome.SCHEDULED


@pytest.mark.django_db
def test_the_users_active_crawl_expires_with_its_record(redis_cache, user, settings):
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})

    ttl = redis_cache.ttl(seed_crawl._active_key(user.id))

    assert 0 < ttl <= settings.SEED_CRAWL_RECORD_TTL_SECONDS


@pytest.mark.django_db
def test_a_crawl_out_of_attempts_gives_way_to_the_next_due(redis_cache, user, settings):
    other = User.objects.create_user(username="other", email="other@example.com", password="x")
    settings.SEED_CRAWL_LEASE_SECONDS = 0
    settings.SEED_CRAWL_MAX_ATTEMPTS = 1
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})
    _claim(redis_cache)
    seed_crawl.queue_seed_crawl(other.id, "tokio", [SEED], [], {"tokio.rs": 1})

    record_key, _ = _claim(redis_cache)

    assert record_key.decode() == seed_crawl._record_key(other.id, "tokio")
    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "failed"
    assert seed_crawl.get_active_seed_crawl_query(user.id) is None


@pytest.mark.django_db
def test_a_done_crawl_stays_done_if_scoring_its_domains_fails(fake_web, redis_cache, user, index_path, monkeypatch):
    def fail(pages_indexed):
        raise RuntimeError("database gone")

    monkeypatch.setattr(seed_crawl, "record_seed_domain_crawls", fail)
    seed_crawl.queue_seed_crawl(user.id, "tokio", [SEED], [], {"tokio.rs": 1})

    _run_rounds(index_path)

    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "done"
