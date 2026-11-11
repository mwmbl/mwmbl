"""Write the stored Commons files to the search index as ordinary documents (mwmbl/mwmbl#610).

A file is indexed as a page of its own, the way a crawled page is: its canonical Commons page URL is the URL, its
caption (else its cleaned file name) the title and its description the extract, cut to the crawler's lengths. It goes
through index_documents, so it is written under its own terms, never under a query that found it, with the same
write priority and blacklist check as a crawled page. A file with no real text is skipped: its terms would come from
a camera's file name alone, and bland text is also where an unsafe file is hardest to judge.

Each file is taken once for the text it holds (CommonsImage.indexed_text_hash) and again when that text changes.
Revoked files are left out, but the index can't delete a document, so a file revoked after it was written is
dropped at serving time instead.

The writes run in the index_images management command rather than on the store's thread after a search: a run is
bounded by IMAGE_INDEX_LIMIT, can be scheduled, holds the index's page locks outside any web process, and also
takes the text changes the recheck finds, which no search sees. The command, not this module, checks
IMAGE_INDEXING_ENABLED.

No query reaches the index, but which files the store holds follows from what people searched for, so a file
appearing in the index says that someone's search fetched it, if not who or when.
"""

import re
from collections import Counter

from django.db.models import F

from mwmbl.indexer.index_batches import index_documents
from mwmbl.models import CommonsImage
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


def file_name(title: str) -> str:
    """A file title, such as `Red_panda at  the zoo.jpg`, as words: `Red panda at the zoo`."""
    name = title.rsplit(".", 1)[0]
    return " ".join(name.replace("_", " ").split())


def document_title(row: CommonsImage) -> str:
    return row.caption or file_name(row.title)


def is_camera_name(title: str) -> bool:
    if PANASONIC_PATTERN.fullmatch(title):
        return True
    without_words = CAMERA_WORDS_PATTERN.sub(" ", title).strip()
    match = CAMERA_NAME_PATTERN.fullmatch(without_words)
    if match is None:
        return False
    digits = sum(character.isdigit() for character in match["stamp"])
    min_digits = MIN_BARE_DIGITS if match["prefix"] is None else MIN_PREFIXED_DIGITS
    return digits >= min_digits


def has_real_text(row: CommonsImage) -> bool:
    has_text = bool(row.caption or row.description)
    return has_text and not is_camera_name(document_title(row))


def truncated(text: str, limit: int) -> str:
    # As the crawler cuts a page's title and extract (mwmbl.crawler.app), so that a file takes no more of a page.
    return text if len(text) <= limit else text[: limit - 1] + "…"


def image_document(row: CommonsImage) -> Document:
    title = truncated(document_title(row), NUM_TITLE_CHARS)
    extract = truncated(row.description, NUM_EXTRACT_CHARS)
    last_checked = int(row.last_checked_at.timestamp())
    return Document(title, row.page_url, extract, last_crawled=last_checked)


def index_images(index_path: str, limit: int) -> Counter:
    """Write up to `limit` approved files whose text the index hasn't taken yet. Counts the files the index stored,
    those it dropped (its pages full of higher-priority documents, or the blacklist) and those skipped for having
    no real text."""
    pending = CommonsImage.objects.filter(revoked_at=None).exclude(indexed_text_hash=F("text_hash"))
    rows = list(pending.order_by("id")[:limit])
    documents = [image_document(row) for row in rows if has_real_text(row)]
    indexed = index_documents(documents, index_path)
    for row in rows:
        # Only while the text is the one just taken and the file is not revoked: a row the store or the recheck
        # changed meanwhile is taken again next run. A dropped file is taken again only when its text changes, as a
        # crawled page would be only when crawled again.
        unchanged = CommonsImage.objects.filter(pk=row.pk, text_hash=row.text_hash, revoked_at=None)
        unchanged.update(indexed_text_hash=row.text_hash)
    num_stored = len(indexed.urls.stored)
    return Counter(stored=num_stored, dropped=len(documents) - num_stored, skipped=len(rows) - len(documents))
