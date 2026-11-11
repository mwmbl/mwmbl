"""Cache keys for what is remembered per image, so an image that comes up again is not judged again.

A cached judgment is only ever reused for exactly what was judged, so a hit is as good as asking
again: the key covers the content judged and a fingerprint of whatever decided the judgment (the
model, the question, the settings), so changing any of them makes every old entry unreachable.

Only judgments are stored, never images, and they expire on their own. Redis may drop any of them
at any time, which costs a re-check and nothing else.

The judge's NSFW scores (see judge.nsfw_scores) are kept this way, keyed by the file's SHA-1, the
exact text the judge was shown and a fingerprint of the prompt and model.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache

# Raise this when a change to the code, rather than a model or a setting, changes judgments.
KEY_PREFIX = "imagesearch:verdict:v1:"
KEY_DIGEST_CHARS = 32


def cache_key(kind: str, fingerprint: str, content: str) -> str:
    digest = hashlib.sha256(f"{fingerprint}\n{content}".encode()).hexdigest()
    return f"{KEY_PREFIX}{kind}:{digest[:KEY_DIGEST_CHARS]}"


def get_scores(keys: list[str]) -> dict[str, float]:
    """The scores held for these keys; a key with none is left out."""
    return cache.get_many(keys)


def put_scores(scores: dict[str, float]) -> None:
    cache.set_many(scores, settings.IMAGE_SEARCH_VERDICT_CACHE_SECONDS)
