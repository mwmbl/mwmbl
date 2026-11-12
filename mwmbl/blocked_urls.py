"""What identifies a blocked URL, shared by the model that stores it and the snapshot that enforces it.

A URL is blocked exactly, never by prefix: a prefix can't be looked up in the snapshot's array of
hashes, and a moderator removing one file wants that file gone, not everything under its path.
A Commons file is keyed by its canonical page URL, so blocking any spelling of it blocks them all.
"""

from mwmbl.imagesearch.commons import commons_file_key

# A domain can't contain a colon, so a hashed URL key never matches a hashed domain.
URL_HASH_PREFIX = "url:"


def url_block_key(url: str) -> str:
    return commons_file_key(url) or url


def url_hash_input(url: str) -> str:
    return URL_HASH_PREFIX + url_block_key(url)
