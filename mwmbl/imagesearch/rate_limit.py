"""A per-client request limit for the image endpoints.

An uncached image search fetches up to thirty thumbnails from Wikimedia and runs two models over
them, and the endpoints are public. Without a limit one client could spend the server's CPU and
Wikimedia's goodwill; Wikimedia rate-limits us as a whole, so one client's burst would also pause
image search for everyone (see commons.get_with_backoff).

Cached answers count too: they are cheap, but a client hammering one query should not get a free
pass, and counting only misses would mean running the pipeline's cache check before the limit.
"""

import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from ninja.errors import HttpError

from mwmbl.search_callers import client_network

WINDOW_SECONDS = 60


def enforce_rate_limit(request: HttpRequest) -> None:
    """Raise a 429 if this client has used up its requests for the current minute."""
    window = int(time.time() // WINDOW_SECONDS)
    key = f"imagesearch:rate:{client_network(request)}:{window}"
    if cache.add(key, 1, timeout=WINDOW_SECONDS):
        return
    try:
        count = cache.incr(key)
    except ValueError:
        # The window's key expired between add() and incr(); this request opens a new count.
        cache.add(key, 1, timeout=WINDOW_SECONDS)
        return
    if count > settings.IMAGE_SEARCH_REQUESTS_PER_MINUTE:
        raise HttpError(429, "Too many image searches. Please wait a minute and try again.")
