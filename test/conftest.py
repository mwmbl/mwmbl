"""Pytest configuration shared across the test suite.

The `blockbuster` fixture installs BlockBuster, which monkey-patches the
stdlib blocking primitives (`socket`, `time.sleep`, file I/O…) and raises
when they are called from a running event loop. Use it in async tests that
exercise code which must not block — particularly Super Search.

We also enable asyncio debug mode and lower `slow_callback_duration` so any
callback that runs longer than 100 ms triggers a warning. Combined with
`-W error::RuntimeWarning` (set per test if desired), this catches most
accidental sync calls leaking into async paths.
"""

import multiprocessing
import os
import time
from typing import NamedTuple

import pytest
from django.conf import settings
from django_redis import get_redis_connection

from mwmbl.apps import create_index
from mwmbl.main import run_index_server, stop_index_server
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

INDEX_SERVER_NUM_PAGES = 64
INDEX_SERVER_PAGE_SIZE = 4096


def pytest_sessionstart(session):
    """Create the test search index before collection, which imports mwmbl.search_setup."""
    create_index(settings.INDEX_NAME, settings.NUM_PAGES)


@pytest.fixture
def blockbuster():
    """Detect blocking calls made from inside the event loop during the test."""
    try:
        from blockbuster import blockbuster_ctx
    except ImportError:  # pragma: no cover
        pytest.skip("blockbuster not installed")
    with blockbuster_ctx() as bb:
        yield bb


@pytest.fixture
def async_debug_loop(event_loop):
    """Enable asyncio debug mode and a strict slow-callback threshold."""
    event_loop.set_debug(True)
    event_loop.slow_callback_duration = 0.1
    yield event_loop


@pytest.fixture
def redis_cache(settings):
    """Point the default cache at a real Redis, emptied before and after the test.

    The test settings use LocMemCache, which can't run the Lua scripts some quota
    counters rely on for atomicity. TEST_REDIS_URL names a Redis database the tests
    may flush, so it must not be one holding data you want to keep.
    """
    redis_url = os.environ["TEST_REDIS_URL"]
    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": redis_url,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
    connection = get_redis_connection("default")
    connection.flushdb()
    yield connection
    connection.flushdb()


class IndexServer(NamedTuple):
    socket_path: str
    index_path: str
    """Served as "main"."""


@pytest.fixture
def index_server(tmp_path):
    """The Rust index server on a new index, started as mwmbl.main starts it."""
    index_path = str(tmp_path / "index.tinysearch")
    socket_path = str(tmp_path / "index.sock")
    TinyIndex.create(Document, index_path, num_pages=INDEX_SERVER_NUM_PAGES, page_size=INDEX_SERVER_PAGE_SIZE)
    args = (os.getpid(), socket_path, [("main", index_path)], 2)
    process = multiprocessing.get_context("spawn").Process(target=run_index_server, args=args)
    process.start()
    deadline = time.monotonic() + 30
    while not os.path.exists(socket_path):
        assert process.is_alive() and time.monotonic() < deadline, "The index server did not start"
        time.sleep(0.05)
    yield IndexServer(socket_path, index_path)
    stop_index_server(process, 10)
    assert process.exitcode == 0
