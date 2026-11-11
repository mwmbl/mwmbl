"""Commons files in search results, served only as the store vouches for them (mwmbl/mwmbl#610).

The index can't delete a document, so a file revoked after it was indexed stays in the index; it is kept out of
results here instead. A result that is a Commons file page is shown only if the store holds the file, unrevoked and
rechecked within IMAGE_SERVE_MAX_CHECK_AGE. Any other Commons file page, including one the crawler found and the
judge never saw, is dropped, and the results below it move up.

A file that is shown becomes an ImageDocument, carrying its thumbnail and licence to the formatting, and the title
and extract the judge read rather than whatever the index holds under its URL, which a crawler may have written.

ServedImagesRanker does this to the ranked list, so it goes inside anything that counts the images in it. It makes
one query per search, and none when no result is a Commons file page. The query runs on the thread that ranks, so
an async view must rank with sync_to_async, whose thread Django cleans up after the request, not asyncio.to_thread.
"""

from datetime import datetime, timezone

from django.conf import settings

from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.imagesearch.indexing import document_title, truncated
from mwmbl.imagesearch.types import ImageDocument
from mwmbl.models import CommonsImage
from mwmbl.settings import NUM_EXTRACT_CHARS, NUM_TITLE_CHARS
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import Retrieval


def servable_rows(keys: set[str]) -> dict[str, CommonsImage]:
    """The stored files among `keys` that may be shown, by key."""
    checked_after = datetime.now(timezone.utc) - settings.IMAGE_SERVE_MAX_CHECK_AGE
    servable = CommonsImage.objects.filter(revoked_at__isnull=True, last_checked_at__gte=checked_after)
    return servable.in_bulk(list(keys), field_name="page_url")


def image_document(result: Document, row: CommonsImage) -> ImageDocument:
    image = {
        "thumbnail_url": row.thumbnail_url,
        "thumbnail_width": row.thumbnail_width,
        "thumbnail_height": row.thumbnail_height,
        "license_name": row.license_name,
        "license_url": row.license_url,
        "attribution": row.attribution,
    }
    title = truncated(document_title(row), NUM_TITLE_CHARS)
    extract = truncated(row.description, NUM_EXTRACT_CHARS)
    return ImageDocument(
        title, result.url, extract, result.score, result.term, result.state, source=result.source, image=image
    )


def served(results: list[Document]) -> list[Document]:
    """The results without the Commons file pages that may not be shown, and with those that may as ImageDocuments.

    A file reached by two URLs (the mobile site, say) is shown once, where it ranks highest.
    """
    keys = {result.url: commons_file_key(result.url) for result in results}
    file_keys = {key for key in keys.values() if key is not None}
    if not file_keys:
        return results
    rows = servable_rows(file_keys)
    kept = []
    shown_keys = set()
    for result in results:
        key = keys[result.url]
        if key is None:
            kept.append(result)
        elif key in rows and key not in shown_keys:
            kept.append(image_document(result, rows[key]))
            shown_keys.add(key)
    return kept


class ServedImagesRanker:
    """Decorator that leaves out of a ranker's search results every Commons file the store doesn't vouch for.

    Delegates retrieval, completion and raw results unchanged.
    """

    def __init__(self, ranker):
        self.ranker = ranker

    def search(self, s: str, additional_results: list[Document], use_external_search: bool = True) -> list[Document]:
        return served(self.ranker.search(s, additional_results, use_external_search))

    def retrieve(self, q: str) -> Retrieval:
        return self.ranker.retrieve(q)

    def search_retrieved(self, retrieval: Retrieval, additional_results: list[Document]) -> list[Document]:
        return served(self.ranker.search_retrieved(retrieval, additional_results))

    def complete(self, q: str):
        return self.ranker.complete(q)

    def get_raw_results(self, query: str):
        return self.ranker.get_raw_results(query)
