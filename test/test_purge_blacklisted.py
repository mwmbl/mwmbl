import pytest

from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.index_batches import index_pages, index_results_against_query, preprocess_documents
from mwmbl.indexer.purge_blacklisted import pages_for_document, purge_documents
from mwmbl.tinysearchengine.indexer import Document, PAGE_SIZE, TinyIndex

NUM_PAGES = 32

BAD = Document(title="Bad page about apples", url="https://badsite.test/apples",
               extract="apples and oranges and bananas", score=1.0)
GOOD = Document(title="Good page about apples", url="https://example.test/apples",
                extract="apples and oranges and bananas", score=1.0)


@pytest.fixture
def index_path(tmp_path):
    path = str(tmp_path / "purge.tinysearch")
    TinyIndex.create(item_factory=Document, index_path=path, num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    return path


def index(index_path, documents):
    index_pages(index_path, preprocess_documents(documents, index_path))


def urls_in_index(index_path):
    with TinyIndex(Document, index_path, 'r') as index:
        return [d.url for page in range(NUM_PAGES) for d in index.get_page(page)]


def test_purge_removes_a_document_from_every_page_it_is_filed_under(index_path):
    index(index_path, [BAD, GOOD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        # The document must be filed under several terms, or this proves nothing.
        assert len(pages_for_document(tiny_index, BAD)) > 1
        removed = purge_documents(tiny_index, [BAD])

    # One document removed, however many pages it took to do it.
    assert removed == {"badsite.test": 1}
    assert "https://badsite.test/apples" not in urls_in_index(index_path)


def test_purge_leaves_other_documents_on_shared_pages_alone(index_path):
    """The bad and good documents share almost all their tokens, so they share pages.
    Purging must remove by URL, not by re-testing the page against the blacklist -
    otherwise it sweeps up documents it was never asked to touch."""
    index(index_path, [BAD, GOOD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        shared = pages_for_document(tiny_index, BAD) & pages_for_document(tiny_index, GOOD)
        assert shared, "expected these documents to share at least one page"
        purge_documents(tiny_index, [BAD])

    urls = urls_in_index(index_path)
    assert "https://badsite.test/apples" not in urls
    assert "https://example.test/apples" in urls


def test_purge_re_checks_the_blacklist_before_removing(index_path):
    """A domain taken off the blacklist between being queued and being purged must
    survive."""
    index(index_path, [BAD, GOOD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        removed = purge_documents(tiny_index, [BAD], is_blacklisted=lambda domain: False)

    assert removed == {}
    assert "https://badsite.test/apples" in urls_in_index(index_path)


def test_purge_removes_only_the_documents_that_are_still_blacklisted(index_path):
    index(index_path, [BAD, GOOD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        removed = purge_documents(tiny_index, [BAD, GOOD],
                                  is_blacklisted=lambda domain: domain == "badsite.test")

    assert set(removed) == {"badsite.test"}
    urls = urls_in_index(index_path)
    assert "https://badsite.test/apples" not in urls
    assert "https://example.test/apples" in urls


def test_purge_of_an_absent_document_is_a_no_op(index_path):
    index(index_path, [GOOD])
    absent = Document(title="Nowhere", url="https://badsite.test/nowhere", extract="nothing", score=1.0)

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        assert purge_documents(tiny_index, [absent]) == {}

    assert "https://example.test/apples" in urls_in_index(index_path)


def test_pages_for_document_matches_the_indexer(index_path):
    """purge_documents relies on tokenize_document being a pure function of the stored
    fields, so that recomputing it recovers exactly the pages the indexer wrote to."""
    with TinyIndex(Document, index_path, 'r') as tiny_index:
        tokens = tokenize_document(BAD.url, BAD.title, BAD.extract, BAD.score).tokens
        expected = {tiny_index.get_key_page_index(token) for token in tokens}
        assert pages_for_document(tiny_index, BAD) == expected


# ---------------------------------------------------------------------------
# Copies filed under a term rather than under the document's own tokens
# ---------------------------------------------------------------------------
#
# index_results_against_query files a document under the *query's* terms, and curation
# files it under the curated term. Those terms are arbitrary, so recomputing
# tokenize_document() does not find those pages: without the term on the document, the
# purge removes nothing and the next query re-queues it, forever.

def test_purge_removes_the_copy_filed_under_a_query_term(index_path):
    # index_results_against_query matches a query term against the document's *full* token
    # set, but a document is filed under only the first 10 tokens (plus bigrams). "zebra"
    # is past that cut, so it names a page that recomputing tokenize_document never finds.
    wordy = Document(title="Bad page about apples", url="https://badsite.test/apples",
                     extract="apples and oranges and bananas here are many more words "
                             "including zebra", score=1.0)
    assert "zebra" not in tokenize_document(wordy.url, wordy.title, wordy.extract, wordy.score).tokens

    index_results_against_query([wordy], "zebra", index_path)

    with TinyIndex(Document, index_path, 'r') as tiny_index:
        term_page = tiny_index.get_key_page_index("zebra")
        assert [d.url for d in tiny_index.get_page(term_page)] == [wordy.url]

    queued = Document(title=wordy.title, url=wordy.url, extract=wordy.extract,
                      score=wordy.score, term="zebra")
    with TinyIndex(Document, index_path, 'w') as tiny_index:
        assert purge_documents(tiny_index, [queued]) == {"badsite.test": 1}

    assert wordy.url not in urls_in_index(index_path)


def test_a_document_with_no_term_still_purges_by_its_tokens(index_path):
    index(index_path, [BAD, GOOD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        assert purge_documents(tiny_index, [BAD]) == {"badsite.test": 1}

    assert BAD.url not in urls_in_index(index_path)


def test_removal_counts_are_per_url_not_per_page(index_path):
    """A document lives on a dozen or more pages; counting each removal would report an
    order of magnitude more documents purged than there were."""
    index(index_path, [BAD])

    with TinyIndex(Document, index_path, 'w') as tiny_index:
        assert len(pages_for_document(tiny_index, BAD)) > 1
        assert purge_documents(tiny_index, [BAD]) == {"badsite.test": 1}


def test_pages_for_document_includes_the_term_page(index_path):
    with TinyIndex(Document, index_path, 'r') as tiny_index:
        without_term = pages_for_document(tiny_index, BAD)
        with_term = pages_for_document(
            tiny_index,
            Document(title=BAD.title, url=BAD.url, extract=BAD.extract, score=BAD.score,
                     term="wibble"))

        assert with_term == without_term | {tiny_index.get_key_page_index("wibble")}
