# The version the server's own fetches (mwmbl/crawler/retrieve.py) name in their User-Agent,
# frozen at the last release of the Python crawler. The crawler volunteers run is the Rust one
# in rust/crawler, whose release version is in its Cargo.toml (mwmbl/crawler/release_check.py).
CRAWLER_VERSION: str = "1.1.3"


def parse_release_version(version: str) -> tuple[int, int, int] | None:
    """Parse an X.Y.Z release version, returning None for anything else.

    ASCII digits only, at most six per part: int() also accepts non-ASCII digits that release
    tags never contain.
    """
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isascii() and part.isdigit() and len(part) <= 6 for part in parts):
        return None
    major, minor, patch = parts
    return int(major), int(minor), int(patch)
