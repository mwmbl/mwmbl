"""Verdicts remembered per image, so an image that comes up again is not judged again.

The text model is the most expensive step of image search, and the thumbnail fetch and NudeNet
the next, and the same images turn up in many queries. A cached verdict is only ever reused for
exactly what was judged, so a hit is as good as running the model:

- a text verdict is keyed by the text itself, so an edited description or category is a new key;
- a pixel verdict is keyed by the file's SHA-1 from Commons, so a file replaced after it was
  approved is a new key and gets a fresh check. This is stronger than keying by thumbnail URL,
  which is not tied to a revision;
- both keys include a fingerprint of the model files and the settings that decide a verdict,
  so retraining a model or moving a threshold makes every old entry unreachable.

A pixel verdict is cached only for a file old enough that its thumbnail cannot still be the previous
file's (see pipeline.pass_pixel_checks). A SHA-1 collision between a harmless and an explicit file
would defeat the pixel key; that is accepted, since it takes an attacker who can craft a collision
and get both files onto Commons.

Only verdicts are stored, never text or images, and they expire on their own. Redis may drop any
of them at any time, which costs a re-check and nothing else.
"""

import hashlib
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from mwmbl.imagesearch.types import Verdict

# Raise this when a change to the code, rather than a model or a setting, changes verdicts.
KEY_PREFIX = "imagesearch:verdict:v1:"
KEY_DIGEST_CHARS = 32


def digest_files(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        with path.open("rb") as f:
            digest.update(hashlib.file_digest(f, "sha256").digest())
    return digest.hexdigest()


def cache_key(kind: str, fingerprint: str, content: str) -> str:
    digest = hashlib.sha256(f"{fingerprint}\n{content}".encode()).hexdigest()
    return f"{KEY_PREFIX}{kind}:{digest[:KEY_DIGEST_CHARS]}"


def get_verdicts(keys: list[str]) -> dict[str, Verdict]:
    """The verdicts held for these keys; a key with none is left out."""
    return {key: Verdict(value) for key, value in cache.get_many(keys).items()}


def put_verdicts(verdicts: dict[str, Verdict]) -> None:
    cache.set_many(
        {key: verdict.value for key, verdict in verdicts.items()}, settings.IMAGE_SEARCH_VERDICT_CACHE_SECONDS
    )
