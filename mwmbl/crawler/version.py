# No imports: the crawler release workflow runs this file with a bare python3 (runpy), without
# installing the project. Raising the version on main publishes a release; CI fails a pull
# request that changes the crawler without raising it (mwmbl/crawler/release_check.py).
CRAWLER_VERSION: str = "1.0.4"


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
