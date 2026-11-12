"""The image strip, and what a search result needs to be shown in it, derived from the indexed document alone.

Standard search must not read Postgres or call out, so nothing here looks anything up: the licence is
the document's `source` and the thumbnail's address is a function of the file name.
"""

import hashlib
from urllib.parse import quote, unquote

from django.conf import settings
from ninja import Field, Schema

from mwmbl.imagesearch.commons import FILE_PAGE_PREFIX, THUMBNAIL_WIDTH, commons_file_key
from mwmbl.imagesearch.licences import LICENCE_NAMES_AND_URLS

# Only the results a searcher sees first are worth promoting to the strip.
IMAGE_STRIP_WINDOW = 10
THUMBNAIL_URL_PREFIX = "https://upload.wikimedia.org/wikipedia/commons/thumb"
# Thumbnails keep the file's own format, except an SVG, which is rendered to PNG.
THUMBNAIL_SUFFIXES = {"jpg": "", "jpeg": "", "png": "", "webp": "", "svg": ".png"}
# Past this many bytes Commons names the thumbnail `{width}px-thumbnail.{extension}` instead of repeating the name.
# Measured on 16,570 Commons files: every name up to 160 bytes repeats it and every name from 161 does not.
MAX_REPEATED_NAME_BYTES = 160


class ImageResult(Schema):
    title: str = Field(description="The file name on Commons. Plain text, but not HTML-safe: escape it.")
    page_url: str = Field(
        description="The file's page on Commons, which names the author and has the full-size file. Link every image "
        "to it: that link is its credit, and most licences require one."
    )
    thumbnail_url: str = Field(
        description='Load this directly from Commons, with `referrerpolicy="no-referrer"`: Mwmbl does not proxy '
        "or store images. Wikimedia sees the viewer's IP address. Hide the image if it fails to load: a file "
        "narrower than the thumbnail may have none."
    )
    source: str = Field(examples=["commons"])
    license_name: str = Field(description="The licence the file is under, such as `CC BY-SA 4.0`. Show it.")
    license_url: str | None = Field(
        description="The licence's page; null when it is unknown. Set by the uploader for Seed Search images. "
        'Link it with `rel="noopener noreferrer nofollow"`.'
    )


def thumbnail_url(file_name: str, width: int = THUMBNAIL_WIDTH) -> str | None:
    """Where Commons serves a `width`-pixel thumbnail of a file, from its name (spaces as underscores).

    The directory is the start of the MD5 of the name. None for a format whose thumbnail name we can't derive
    confidently (TIFF, PDF, video and so on), which is shown as a plain text result.
    """
    stem, dot, extension = file_name.rpartition(".")
    extension = extension.lower()
    # A name with no dot has no extension: `File:Png` is not a PNG.
    if not stem or not dot or extension not in THUMBNAIL_SUFFIXES:
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


def image_for_result(result) -> ImageResult | None:
    """The strip image for a hit: set only for documents our own indexing wrote with a Commons licence as the
    source. A crawled Commons page has no source and stays a web result, because nothing vetted it."""
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
    return ImageResult(
        title=file_name.replace("_", " "),
        page_url=page_url,
        thumbnail_url=thumbnail,
        source="commons",
        license_name=licence_name,
        license_url=licence_url or None,
    )


def build_image_strip(results: list, leading: tuple[ImageResult, ...] = ()) -> tuple[list[ImageResult], list]:
    """The strip and the web results, from ranked results: `leading` images, then Commons files from the top of
    `results` up to the strip's size, taken out of the web results. A file already in the strip is not shown twice."""
    images = list(leading)
    shown_files = {key for key in (commons_file_key(image.page_url) for image in images) if key is not None}
    for result in results[:IMAGE_STRIP_WINDOW]:
        if len(images) >= settings.IMAGES_DISPLAY_COUNT:
            break
        image = image_for_result(result)
        # image_for_result has checked that the URL is a Commons file page, so the image's page URL is its key.
        if image is not None and image.page_url not in shown_files:
            shown_files.add(image.page_url)
            images.append(image)
    if not shown_files:
        return images, results
    # A None key (not a Commons file) is never in shown_files, so no other result is dropped.
    web_results = [result for result in results if commons_file_key(result.url) not in shown_files]
    return images, web_results
