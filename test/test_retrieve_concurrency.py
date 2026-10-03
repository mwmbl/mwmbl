"""
The crawler's fetch path: one shared SSL context, a session per thread, and batches crawled
concurrently with a per-thread delay.
"""

import ssl
import threading

import fakeredis
import requests
from urllib3 import HTTPSConnectionPool

from mwmbl.crawler import retrieve
from mwmbl.crawler.retrieve import SharedContextAdapter, crawl_batch, get_session


def test_adapter_keeps_verification_but_not_the_bundle_path():
    adapter = SharedContextAdapter()
    conn = HTTPSConnectionPool("example.com")

    adapter.cert_verify(conn, "https://example.com/", verify=True, cert=None)

    assert conn.cert_reqs == "CERT_REQUIRED"
    assert conn.ca_certs is None
    assert conn.ca_cert_dir is None


def test_shared_context_verifies_certificates():
    assert retrieve.SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert retrieve.SSL_CONTEXT.check_hostname


def test_sessions_use_the_shared_context():
    pool = (
        get_session()
        .get_adapter("https://example.com/")
        .get_connection_with_tls_context(requests.Request("GET", "https://example.com/").prepare(), verify=True)
    )

    assert pool.conn_kw["ssl_context"] is retrieve.SSL_CONTEXT


def test_session_is_per_thread():
    sessions_by_thread = []
    thread = threading.Thread(target=lambda: sessions_by_thread.append(get_session()))
    thread.start()
    thread.join()

    assert get_session() is get_session()
    assert sessions_by_thread[0] is not get_session()


def test_crawl_batch_crawls_concurrently_and_keeps_order(monkeypatch):
    # Every crawl waits for all three to be in flight at once, so this only finishes if
    # they really run in parallel.
    all_in_flight = threading.Barrier(3, timeout=5)

    def fake_crawl_url(url, redis):
        all_in_flight.wait()
        return {"url": url}

    monkeypatch.setattr(retrieve, "crawl_url", fake_crawl_url)
    urls = ["https://a.test/", "https://b.test/", "https://c.test/"]

    results = crawl_batch(urls, num_threads=3, delay_seconds=0.0, redis=fakeredis.FakeStrictRedis())

    assert [result["url"] for result in results] == urls


def test_crawl_batch_delays_between_urls_on_each_thread(monkeypatch):
    sleeps = []
    monkeypatch.setattr(retrieve, "crawl_url", lambda url, redis: {"url": url})
    monkeypatch.setattr(retrieve.time, "sleep", sleeps.append)
    urls = ["https://a.test/", "https://b.test/", "https://c.test/"]

    crawl_batch(urls, num_threads=1, delay_seconds=2.0, redis=fakeredis.FakeStrictRedis())

    assert len(sleeps) == 2
    assert all(1.8 <= seconds <= 2.2 for seconds in sleeps)


def test_crawl_batch_without_delay_never_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(retrieve, "crawl_url", lambda url, redis: {"url": url})
    monkeypatch.setattr(retrieve.time, "sleep", sleeps.append)

    crawl_batch(
        ["https://a.test/", "https://b.test/"], num_threads=1, delay_seconds=0.0, redis=fakeredis.FakeStrictRedis()
    )

    assert sleeps == []


def test_crawl_batch_counts_fetches_overlapping_on_a_domain(monkeypatch):
    # Both crawls of a.test wait for each other, so the second always starts while the
    # first is in flight; b.test never overlaps.
    both_a_in_flight = threading.Barrier(2, timeout=5)

    def fake_crawl_url(url, redis):
        if "a.test" in url:
            both_a_in_flight.wait()
        return {"url": url}

    monkeypatch.setattr(retrieve, "crawl_url", fake_crawl_url)
    redis = fakeredis.FakeStrictRedis()
    urls = ["https://a.test/1", "https://a.test/2", "https://b.test/"]

    crawl_batch(urls, num_threads=3, delay_seconds=0.0, redis=redis)

    totals = redis.hgetall(retrieve.DOMAIN_OVERLAP_KEY)
    assert totals == {b"fetches": b"3", b"overlapped": b"1", b"duplicate_domains": b"1"}
    assert int(redis.get(retrieve.DOMAIN_IN_FLIGHT_KEY.format(domain="a.test"))) == 0
