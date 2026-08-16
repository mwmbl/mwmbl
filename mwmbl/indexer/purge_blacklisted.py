"""Removal of specific already-indexed documents from the index.

TinyIndex has no per-document delete: a page is a compressed blob of documents, so
removal means reading the page, dropping the document from the list, and writing the
whole page back. The only hard part is knowing *which* pages to rewrite.

A document is filed under every token that tokenize_document() derives from it - each
token hashes to one page - so a document typically lives on a dozen or more pages, and
removing it from just the page you happened to find it on leaves it retrievable by every
other term. tokenize_document() is a pure function of the url/title/extract/score stored
in the index, so recomputing it recovers that full token set exactly.
"""
from collections import defaultdict
from logging import getLogger
from typing import Callable, Iterable, Optional

from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.utils import get_domain

logger = getLogger(__name__)


def pages_for_document(index: TinyIndex, document: Document) -> set[int]:
    """Every page this document is filed under."""
    tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
    return {index.get_key_page_index(token) for token in tokenized.tokens}


def purge_documents(index: TinyIndex, documents: Iterable[Document],
                    is_blacklisted: Optional[Callable[[str], bool]] = None) -> dict[str, int]:
    """Remove exactly these documents, by URL, from every page they are filed under.

    Returns a count of removed documents by domain.

    Removal is by URL membership, never by re-applying the blacklist to the whole page.
    Common tokens - "com", "you", "the" - hash to pages shared with thousands of
    unrelated documents, so re-testing the page would sweep away blacklisted documents
    that were never in the set we were asked to remove and that nobody has looked at.
    Those will come round again on their own once a query surfaces them.

    `is_blacklisted` re-checks each document before removal, so a domain that came off
    the blacklist between being queued and being purged is left alone. Pass None to skip
    the re-check and remove everything given.
    """
    target_urls = set()
    pages_to_urls: dict[int, set[str]] = defaultdict(set)
    for document in documents:
        if is_blacklisted is not None:
            try:
                domain = get_domain(document.url)
            except ValueError:
                continue
            if not is_blacklisted(domain):
                logger.info("Skipping purge of %s: no longer blacklisted", document.url)
                continue

        target_urls.add(document.url)
        for page_index in pages_for_document(index, document):
            pages_to_urls[page_index].add(document.url)

    removed_by_domain: dict[str, int] = defaultdict(int)
    for page_index, urls in pages_to_urls.items():
        page = index.get_page(page_index)
        kept = [d for d in page if d.url not in urls]
        if len(kept) == len(page):
            continue

        for document in page:
            if document.url in urls:
                try:
                    removed_by_domain[get_domain(document.url)] += 1
                except ValueError:
                    removed_by_domain[document.url] += 1
        index.store_in_page(page_index, kept)

    if removed_by_domain:
        logger.info("Purged %d documents across %d pages from %d URLs",
                    sum(removed_by_domain.values()), len(pages_to_urls), len(target_urls))
    return dict(removed_by_domain)
