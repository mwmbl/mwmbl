"""What a standard-search result needs to be shown as an image, derived from the indexed document alone.

Standard search must not read Postgres or call out, so nothing here looks anything up: the licence is
the document's `source` and the thumbnail's address is a function of the file name.
"""

import hashlib
from urllib.parse import quote, unquote

from mwmbl.imagesearch.commons import FILE_PAGE_PREFIX, commons_file_key
from mwmbl.imagesearch.licences import LICENCE_NAMES_AND_URLS

THUMBNAIL_WIDTH = 330
THUMBNAIL_URL_PREFIX = "https://upload.wikimedia.org/wikipedia/commons/thumb"
# Thumbnails keep the file's own format, except an SVG, which is rendered to PNG.
THUMBNAIL_SUFFIXES = {"jpg": "", "jpeg": "", "png": "", "webp": "", "svg": ".png"}
# Past this many bytes Commons names the thumbnail `{width}px-thumbnail.{extension}` instead of repeating the name.
# Measured on 16,570 Commons files: every name up to 160 bytes repeats it and every name from 161 does not.
MAX_REPEATED_NAME_BYTES = 160


def thumbnail_url(file_name: str, width: int = THUMBNAIL_WIDTH) -> str | None:
    """Where Commons serves a `width`-pixel thumbnail of a file, from its name (spaces as underscores).

    The directory is the start of the MD5 of the name. None for a format whose thumbnail name we can't derive
    confidently (TIFF, PDF, video and so on), which is shown as a plain text result.
    """
    extension = file_name.rsplit(".", 1)[-1].lower()
    if extension not in THUMBNAIL_SUFFIXES:
        return None
    name_bytes = file_name.encode("utf-8")
    suffix = THUMBNAIL_SUFFIXES[extension]
    # The long-name rule is only confirmed for formats that keep their extension; an SVG's isn't seen, so no image.
    if len(name_bytes) > MAX_REPEATED_NAME_BYTES and suffix:
        return None
    digest = hashlib.md5(name_bytes).hexdigest()
    quoted = quote(file_name, safe="")
    thumbnail_name = f"thumbnail.{extension}" if len(name_bytes) > MAX_REPEATED_NAME_BYTES else quoted + suffix
    return f"{THUMBNAIL_URL_PREFIX}/{digest[0]}/{digest[:2]}/{quoted}/{width}px-{thumbnail_name}"


def image_for_result(result) -> dict | None:
    """The `image` of a search hit: set only for documents our own indexing wrote with a Commons licence as the
    source. A crawled Commons page has no source and stays a text result, because nothing vetted it."""
    # An int comparison on the hot path; the work below runs only for Commons images.
    if result.source not in LICENCE_NAMES_AND_URLS:
        return None
    page_url = commons_file_key(result.url)
    if page_url is None:
        return None
    file_name = unquote(page_url.removeprefix(FILE_PAGE_PREFIX))
    thumbnail = thumbnail_url(file_name)
    if thumbnail is None:
        return None
    licence_name, licence_url = LICENCE_NAMES_AND_URLS[result.source]
    return {
        "thumbnail_url": thumbnail,
        "license_name": licence_name,
        "license_url": licence_url or None,
        "page_url": page_url,
    }
