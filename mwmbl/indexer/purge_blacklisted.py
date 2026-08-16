"""Removal of specific already-indexed documents from the index.

TinyIndex has no per-document delete: a page is a compressed blob of documents, so
removal means reading the page, dropping the document from the list, and writing the
whole page back. The only hard part is knowing *which* pages to rewrite.

A document is filed under every token that tokenize_document() derives from it - each
token hashes to one page - so a document typically lives on a dozen or more pages, and
removing it from just the page you happened to find it on leaves it retrievable by every
other term. tokenize_document() is a pure function of the url/title/extract/score stored
in the index, so recomputing it recovers that full token set exactly.

Not every copy of a document comes from its own tokens, though. index_results_against_query
files documents under the *query's* unigrams and bigrams, and curation files them under the
curated term; those terms are arbitrary and unrecoverable from the document's text. Each
such copy carries the term it was filed under, so the copy that retrieval surfaced is
removed along with the token pages. Other copies of the same URL under other terms are left
for the queries that surface them, which is what makes the loop converge: every purge
removes at least the copy that was queued, so no document can churn through the queue
forever.
"""
from collections import defaultdict
from logging import getLogger
from typing import Callable, Iterable, Optional

from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.utils import get_domain

logger = getLogger(__name__)


def pages_for_document(index: TinyIndex, document: Document) -> set[int]:
    """Every page this copy of the document is filed under.

    The document's own tokens, plus the term it was filed under if it has one - a query
    term or a curated term, neither of which is derivable from the document's text.
    """
    tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
    pages = {index.get_key_page_index(token) for token in tokenized.tokens}
    if document.term:
        pages.add(index.get_key_page_index(document.term))
    return pages


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

    # Counted by URL, not by (page, document): one document occupies a dozen or more
    # pages, so counting each removal would report an order of magnitude too many.
    removed_urls_by_domain: dict[str, set[str]] = defaultdict(set)
    for page_index, urls in pages_to_urls.items():
        page = index.get_page(page_index)
        kept = [d for d in page if d.url not in urls]
        if len(kept) == len(page):
            continue

        for document in page:
            if document.url in urls:
                try:
                    removed_urls_by_domain[get_domain(document.url)].add(document.url)
                except ValueError:
                    removed_urls_by_domain[document.url].add(document.url)
        index.store_in_page(page_index, kept)

    removed_by_domain = {domain: len(urls) for domain, urls in removed_urls_by_domain.items()}
    if removed_by_domain:
        logger.info("Purged %d documents across %d pages from %d URLs",
                    sum(removed_by_domain.values()), len(pages_to_urls), len(target_urls))
    return removed_by_domain
