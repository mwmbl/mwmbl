"""The options that make the crawler safe to run somewhere that is not a volunteer's machine.

A CI smoke test needs a crawl that is bounded (a handful of URLs), aimed at sites we chose,
and unable to write into the production index. These cover the three knobs that give it that,
plus the one-shot mode that turns the run into an exit code.
"""

from datetime import datetime, timezone
from random import Random
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from mwmbl.crawl import Crawler, validate_environment
from mwmbl.crawler.urls import FoundURL, URLStatus
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.utils import parse_url


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True, health_check_interval=30)


@pytest.fixture
def blacklist_provider():
    return StaticBlacklistProvider({"spam.com"})


@pytest.fixture
def url_queue(fake_redis, blacklist_provider):
    return RedisURLQueue(fake_redis, lambda: set(), blacklist_provider)


def found_url(url: str) -> FoundURL:
    return FoundURL(
        url=url,
        user_id_hash="test_user_hash",
        status=URLStatus.NEW,
        timestamp=datetime.now(timezone.utc),
        last_crawled=None,
    )


# ---------------------------------------------------------------------------
# CRAWL_BATCH_SIZE
# ---------------------------------------------------------------------------


def test_batch_size_bounds_the_number_of_urls(url_queue):
    """CI wants a crawl of ten URLs, not a hundred."""
    url_queue.queue_urls([found_url(f"https://example{i}.com/page") for i in range(50)])

    with patch("mwmbl.redis_url_queue.CRAWL_BATCH_SIZE", 10):
        batch = url_queue.get_batch("test_user")

    # The seed URL is appended before the loop checks the bound, so ten is the last size
    # at which another domain can still be added.
    assert len(batch) <= 11, f"batch of {len(batch)} ignored CRAWL_BATCH_SIZE"


def test_batch_size_defaults_to_the_unbounded_behaviour():
    """The default has to stay what volunteer crawlers have always run with."""
    from mwmbl.crawler.env_vars import CRAWL_BATCH_SIZE

    assert CRAWL_BATCH_SIZE == 100


# ---------------------------------------------------------------------------
# CRAWL_ALLOWED_DOMAINS
# ---------------------------------------------------------------------------


def test_allowed_domains_restrict_the_crawl(url_queue):
    """Every URL in the batch, seed included, must come from the allowlist."""
    url_queue.queue_urls([found_url(f"https://example{i}.com/page") for i in range(20)])
    allowed = frozenset({"mwmbl.org", "en.wikipedia.org"})

    with patch("mwmbl.redis_url_queue.CRAWL_ALLOWED_DOMAINS", allowed):
        batch = url_queue.get_batch("test_user")

    assert batch, "the allowlist produced no URLs at all"
    crawled_domains = {parse_url(url).netloc for url in batch}
    assert crawled_domains <= allowed, f"crawled outside the allowlist: {crawled_domains - allowed}"


def test_allowed_domains_still_honour_the_blacklist(fake_redis, blacklist_provider):
    """An allowlisted domain that is blacklisted must not be crawled."""
    queue = RedisURLQueue(fake_redis, lambda: set(), blacklist_provider)

    with patch("mwmbl.redis_url_queue.CRAWL_ALLOWED_DOMAINS", frozenset({"spam.com", "mwmbl.org"})):
        batch = queue.get_batch("test_user")

    assert not any(parse_url(url).netloc == "spam.com" for url in batch)


def test_allowed_domains_pick_the_seed_deterministically(url_queue):
    """The crawler image randomises PYTHONHASHSEED, so set ordering must not pick the seed URL.

    Two crawler processes given the same allowlist have to seed from the same domain, which
    means choosing from a sorted sequence rather than from the set itself.
    """
    allowed = frozenset({"mwmbl.org", "en.wikipedia.org", "example.org"})
    expected_domain = Random(1).choice(sorted(allowed))

    with (
        patch("mwmbl.redis_url_queue.CRAWL_ALLOWED_DOMAINS", allowed),
        patch("mwmbl.redis_url_queue.random", Random(1)),
    ):
        batch = url_queue.get_batch("test_user")

    assert batch[0] == f"https://{expected_domain}/"


def test_no_allowlist_leaves_domain_selection_alone(url_queue):
    """The default path must keep sampling the live queue as before."""
    url_queue.queue_urls([found_url(f"https://example{i}.com/page") for i in range(20)])

    batch = url_queue.get_batch("test_user")

    assert batch
    assert not all(parse_url(url).netloc == parse_url(batch[0]).netloc for url in batch)


# ---------------------------------------------------------------------------
# CRAWL_SUBMIT_MODE
# ---------------------------------------------------------------------------


def test_api_key_is_required_when_results_are_submitted():
    with (
        patch("mwmbl.crawl.CRAWL_SUBMIT_MODE", "index"),
        patch("mwmbl.crawl.MWMBL_API_KEY", ""),
        patch("mwmbl.crawl.MWMBL_CONTACT_INFO", "ci@mwmbl.org"),
    ):
        with pytest.raises(ValueError, match="MWMBL_API_KEY"):
            validate_environment()


def test_api_key_is_not_required_when_submission_is_off():
    """Fork pull requests get no secrets, so the keyless run has to be a supported mode."""
    with (
        patch("mwmbl.crawl.CRAWL_SUBMIT_MODE", "off"),
        patch("mwmbl.crawl.MWMBL_API_KEY", ""),
        patch("mwmbl.crawl.MWMBL_CONTACT_INFO", "ci@mwmbl.org"),
    ):
        validate_environment()


def test_contact_info_is_required_whatever_the_submit_mode():
    """We are fetching other people's pages either way."""
    with (
        patch("mwmbl.crawl.CRAWL_SUBMIT_MODE", "off"),
        patch("mwmbl.crawl.MWMBL_API_KEY", ""),
        patch("mwmbl.crawl.MWMBL_CONTACT_INFO", "CHANGE_ME@example.com"),
    ):
        with pytest.raises(ValueError, match="MWMBL_CONTACT_INFO"):
            validate_environment()


# ---------------------------------------------------------------------------
# --once
# ---------------------------------------------------------------------------


def test_run_once_does_one_pass_and_returns():
    crawler = Crawler()

    with (
        patch("mwmbl.crawl.validate_environment"),
        patch.object(Crawler, "check_redis"),
        patch.object(Crawler, "process_batch") as process_batch,
        patch.object(Crawler, "run_indexing") as run_indexing,
    ):
        crawler.run_once()

    process_batch.assert_called_once_with()
    run_indexing.assert_called_once_with()


def test_run_once_propagates_errors_instead_of_restarting():
    """The whole point: the continuous loops swallow this, so a broken crawler looks healthy."""
    crawler = Crawler()

    with (
        patch("mwmbl.crawl.validate_environment"),
        patch.object(Crawler, "check_redis"),
        patch.object(Crawler, "process_batch", side_effect=RuntimeError("crawl failed")),
        patch.object(Crawler, "run_indexing") as run_indexing,
    ):
        with pytest.raises(RuntimeError, match="crawl failed"):
            crawler.run_once()

    run_indexing.assert_not_called()


def test_once_flag_selects_the_single_pass():
    from mwmbl.crawl import main

    crawler = MagicMock()
    with patch("mwmbl.crawl.get_default_crawler", return_value=crawler), patch("sys.argv", ["mwmbl-crawl", "--once"]):
        main()

    crawler.run_once.assert_called_once_with()
    crawler.run.assert_not_called()


def test_no_flag_runs_continuously():
    from mwmbl.crawl import main

    crawler = MagicMock()
    with patch("mwmbl.crawl.get_default_crawler", return_value=crawler), patch("sys.argv", ["mwmbl-crawl"]):
        main()

    crawler.run.assert_called_once_with()
    crawler.run_once.assert_not_called()
