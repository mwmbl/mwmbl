"""Write the Commons files Seed Search judged safe to the search index as ordinary documents (mwmbl/mwmbl#610).

A file is indexed as a page of its own, the way a crawled page is: its canonical Commons page URL is the URL, its
caption (else its cleaned file name, or the start of its description when the name is a camera's) the title, its
description the extract, cut to the crawler's lengths, and its licence the document's `source`. It goes through
index_documents, so it is written under its own terms, never under the query that found it, with the same blacklist
and blocked-URL check as a crawled page. Standard search can then serve it without reading anything but the index.

A file with no real text - no caption, no description and a camera's file name - is skipped: it has nothing to be
found by, and bland text is also where an unsafe file is hardest to judge. So is a file uploaded less than
IMAGE_INDEX_MIN_UPLOAD_AGE ago, so that Commons has had time to delete the spam and copyright violations uploaded to
be found; it is written by a later search that finds it.

Which files are written follows from what people searched for, so a file appearing in the index says that someone's
search found it, if not who or when. The query itself never reaches this module.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.imagesearch.licences import licence_source
from mwmbl.imagesearch.types import SourceImage
from mwmbl.indexer.index_batches import index_documents
from mwmbl.settings import NUM_EXTRACT_CHARS, NUM_TITLE_CHARS
from mwmbl.tinysearchengine.indexer import Document

CAMERA_PREFIX = (
    r"img|imgp|imag|cimg|kimg|dsc|dscn|dscf|dsci|pxl|mvimg|mvi|vid|gopr|dji|sany|hpim|pict|wp|sam|photo|image"
    r"|screenshot|screen shot|whatsapp image"
)
# A camera, phone or app's prefix and a number, or a date and time, and nothing else: IMG_1234, IMG_E1234, DSC_0045,
# IMG-20230101-WA0001, PXL_20230101_123456789~2, DSC01234-Enhanced-NR, Screenshot 2023-01-01 at 12.34.56.
CAMERA_NAME_PATTERN = re.compile(rf"(?:(?P<prefix>{CAMERA_PREFIX})[\s-]*e?)?(?P<stamp>[\d\s.,:()~-]+)", re.IGNORECASE)
# Words cameras and editing apps add around the number.
CAMERA_WORDS_PATTERN = re.compile(r"\b(?:edited|copy|hdr|enhanced|nr|at)\b|\bwa(?=\d)", re.IGNORECASE)
# Panasonic and Olympus: P and seven digits.
PANASONIC_PATTERN = re.compile(r"p\d{7}", re.IGNORECASE)
# Fewer digits than these are as likely a model or a title (P-51, 747, 1984, WP 40) as a camera's count.
MIN_PREFIXED_DIGITS = 3
MIN_BARE_DIGITS = 6
FILE_NAMESPACE = "File:"


def file_name(title: str) -> str:
    """A file title, such as `File:Red_panda at  the zoo.jpg`, as words: `Red panda at the zoo`."""
    name = title.removeprefix(FILE_NAMESPACE).rsplit(".", 1)[0]
    return " ".join(name.replace("_", " ").split())


def is_camera_name(name: str) -> bool:
    if PANASONIC_PATTERN.fullmatch(name):
        return True
    without_words = CAMERA_WORDS_PATTERN.sub(" ", name).strip()
    match = CAMERA_NAME_PATTERN.fullmatch(without_words)
    if match is None:
        return False
    digits = sum(character.isdigit() for character in match["stamp"])
    min_digits = MIN_BARE_DIGITS if match["prefix"] is None else MIN_PREFIXED_DIGITS
    return digits >= min_digits


def has_real_text(image: SourceImage) -> bool:
    return bool(image.caption or image.description) or not is_camera_name(file_name(image.title))


def truncated(text: str, limit: int) -> str:
    # As the crawler cuts a page's title and extract (mwmbl.crawler.app), so that a file takes no more of a page.
    return text if len(text) <= limit else text[: limit - 1] + "…"


def opening_words(text: str, limit: int) -> str:
    """The start of the text, cut at the last word that fits in the limit."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    words = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return words.rstrip(" ,;:.-") + "…"


def document_title(image: SourceImage) -> str:
    name = file_name(image.title)
    if image.caption:
        return truncated(image.caption, NUM_TITLE_CHARS)
    if is_camera_name(name) and image.description:
        return opening_words(image.description, NUM_TITLE_CHARS)
    return truncated(name, NUM_TITLE_CHARS)


def image_document(image: SourceImage, now: datetime) -> Document:
    # Commons' own canonical form, so the same file is one document however a later search found it.
    url = commons_file_key(image.page_url)
    return Document(
        title=document_title(image),
        url=url,
        extract=truncated(image.description, NUM_EXTRACT_CHARS),
        source=licence_source(image.license_name),
        last_crawled=int(now.timestamp()),
    )


def index_images(images: list[SourceImage]) -> int:
    """Write the images worth indexing to the search index, returning how many it stored. The caller has already
    checked they are safe to show."""
    now = datetime.now(timezone.utc)
    uploaded_before = now - settings.IMAGE_INDEX_MIN_UPLOAD_AGE
    old_enough = [image for image in images if image.uploaded_at <= uploaded_before]
    documents = [image_document(image, now) for image in old_enough if has_real_text(image)]
    if not documents:
        return 0
    index_path = str(Path(settings.DATA_PATH) / settings.INDEX_NAME)
    indexed = index_documents(documents, index_path)
    return len(indexed.urls.stored)
