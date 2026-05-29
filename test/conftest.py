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
import asyncio
import sys

import pytest


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
