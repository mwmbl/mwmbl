"""Wikipedia results served from the index instead of the API.

Covers the gate (when a search may skip the Wikipedia call), the privacy rules on what a
stored result may be filed under, and the OverlayIndex the evaluation reads through.
"""
import fcntl
import os
from io import UnsupportedOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from django.test import override_settings

from mwmbl.indexer.index_batches import index_results_against_query
from mwmbl.tinysearchengine.indexer import METADATA_SIZE, Document, DocumentState, TinyIndex
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import HeuristicRanker
from mwmbl.tinysearchengine.wiki_index_cache import (
    count_stored_wiki_results, count_wiki_results, have_enough_wiki_results, per_term_best,
    query_terms, store_wiki_results, wiki_documents_by_term,
)
from mwmbl.tokenizer import get_bigrams, tokenize


NUM_PAGES = 64
RAW_QUERY = "sensitive personal question about someone"


def wiki_doc(title: str, extract: str = "", state=DocumentState.FROM_WIKI, term=RAW_QUERY):
    """A result shaped exactly like get_wiki_results returns: term is the *raw* query."""
    return Document(title, f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    extract, 3.0, term, state=state)


def other_doc(title: str, url: str):
    return Document(title, url, "extract", 1.0, "term")


def identity_rank(documents):
    return documents


@pytest.fixture
def index_path():
    with TemporaryDirectory() as temp_dir:
        path = str(Path(temp_dir) / "temp-index.tinysearch")
        with TinyIndex.create(Document, path, num_pages=NUM_PAGES, page_size=4096):
            pass
        yield path


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def test_count_wiki_results_counts_by_domain():
    documents = [wiki_doc("Bitcoin"), other_doc("Bitcoin", "https://bitcoin.org/"),
                 wiki_doc("Blockchain")]
    assert count_wiki_results(documents) == 2


def test_count_stored_wiki_results_ignores_organically_crawled_wiki_pages():
    crawled = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin", "e", 1.0, "bitcoin")
    assert crawled.state is None
    documents = [crawled, wiki_doc("Blockchain")]

    assert count_wiki_results(documents) == 2
    assert count_stored_wiki_results(documents) == 1


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate", ["raw_candidates", "ranked_top_n", "from_wiki_only"])
def test_gate_fires_at_or_above_threshold(gate):
    documents = [wiki_doc("Bitcoin"), wiki_doc("Blockchain")]

    assert have_enough_wiki_results("bitcoin", documents, identity_rank, gate=gate, threshold=2, top_n=10)
    assert have_enough_wiki_results("bitcoin", documents, identity_rank, gate=gate, threshold=1, top_n=10)
    assert not have_enough_wiki_results("bitcoin", documents, identity_rank, gate=gate, threshold=3, top_n=10)


def test_gate_does_not_fire_on_an_empty_index_result():
    assert not have_enough_wiki_results("bitcoin", [], identity_rank, gate="ranked_top_n",
                                        threshold=1, top_n=10)


def test_ranked_top_n_gate_ignores_wiki_results_below_the_cut():
    # The wiki result is ranked 11th, so a top-10 gate must not count it - the user
    # would never see it, and skipping the call on its account loses a real result.
    documents = [other_doc(f"t{i}", f"https://e{i}.example/") for i in range(10)]
    documents.append(wiki_doc("Bitcoin"))

    assert not have_enough_wiki_results("bitcoin", documents, identity_rank, gate="ranked_top_n",
                                        threshold=1, top_n=10)
    assert have_enough_wiki_results("bitcoin", documents, identity_rank, gate="raw_candidates",
                                    threshold=1, top_n=10)


def test_from_wiki_only_gate_does_not_count_crawled_wiki_pages():
    crawled = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin", "e", 1.0, "bitcoin")

    assert have_enough_wiki_results("bitcoin", [crawled], identity_rank, gate="ranked_top_n",
                                    threshold=1, top_n=10)
    assert not have_enough_wiki_results("bitcoin", [crawled], identity_rank, gate="from_wiki_only",
                                        threshold=1, top_n=10)


def test_never_and_always_gates():
    documents = [wiki_doc("Bitcoin")] * 5
    assert not have_enough_wiki_results("bitcoin", documents, identity_rank, gate="never", threshold=1)
    assert have_enough_wiki_results("bitcoin", [], identity_rank, gate="always", threshold=99)


def test_unknown_gate_is_an_error():
    with pytest.raises(ValueError):
        have_enough_wiki_results("bitcoin", [], identity_rank, gate="nonsense", threshold=1)


def test_gate_does_not_rank_when_it_does_not_need_to():
    # Ranking is the only part of the gate that costs anything, so the cheap gate must
    # not pay for it.
    rank = MagicMock(side_effect=identity_rank)
    have_enough_wiki_results("bitcoin", [wiki_doc("Bitcoin")], rank, gate="raw_candidates",
                             threshold=1)
    rank.assert_not_called()


# ---------------------------------------------------------------------------
# Privacy: what may be written under a query-derived term
# ---------------------------------------------------------------------------

def stored_terms(index_path: str) -> set[str]:
    terms = set()
    with TinyIndex(Document, index_path, 'r') as indexer:
        for page in range(NUM_PAGES):
            terms |= {document.term for document in indexer.get_page(page)}
    return terms


def test_raw_query_is_never_written_to_the_index(index_path):
    # get_wiki_results stamps every result's `term` with the raw, untokenized query. If a
    # write path ever persisted that field as-is, the user's query would land on disk -
    # exactly what removing the filesystem HTTP cache was for. The failure is silent, so
    # it needs its own test.
    documents = [wiki_doc("Someone", extract=RAW_QUERY)]
    assert documents[0].term == RAW_QUERY

    with override_settings(WIKI_INDEX_MAX_TERM_TOKENS=2, WIKI_INDEX_REQUIRE_EXISTING_TERM=False):
        store_wiki_results(RAW_QUERY, documents, index_path)

    terms = stored_terms(index_path)
    assert terms, "nothing was stored, so the test proves nothing"
    assert RAW_QUERY not in terms


def test_stored_terms_are_short_and_contained_in_the_document(index_path):
    query = "bitcoin blockchain ledger"
    document = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin",
                        "A blockchain ledger", 3.0, query, state=DocumentState.FROM_WIKI)

    with override_settings(WIKI_INDEX_MAX_TERM_TOKENS=2, WIKI_INDEX_REQUIRE_EXISTING_TERM=False):
        store_wiki_results(query, [document], index_path)

    document_words = set(tokenize(document.title)) | set(tokenize(document.extract))
    for term in stored_terms(index_path):
        assert len(term.split()) <= 2, f"{term!r} is longer than the token cap"
        assert set(term.split()) <= document_words, f"{term!r} is not contained in the document"


def test_max_term_tokens_excludes_bigrams(index_path):
    query = "bitcoin blockchain"
    document = Document("Bitcoin blockchain", "https://en.wikipedia.org/wiki/Bitcoin",
                        "", 3.0, query, state=DocumentState.FROM_WIKI)

    index_results_against_query([document], query, index_path, max_term_tokens=1)

    assert "bitcoin blockchain" in get_bigrams(2, tokenize(query))
    assert stored_terms(index_path) == {"bitcoin", "blockchain"}


def test_require_existing_term_only_writes_terms_the_index_already_has(index_path):
    # "bitcoin" is a word the corpus already contains; "oxyphenbutazone" is not, so under
    # the strict predicate nothing about it may be written.
    existing = Document("Bitcoin explained", "https://bitcoin.org/", "e", 1.0, "bitcoin")
    with TinyIndex(Document, index_path, 'w') as indexer:
        indexer.store_in_page(indexer.get_key_page_index("bitcoin"), [existing])

    document = Document("Bitcoin oxyphenbutazone", "https://en.wikipedia.org/wiki/Bitcoin",
                        "", 3.0, None, state=DocumentState.FROM_WIKI)
    index_results_against_query([document], "bitcoin oxyphenbutazone", index_path,
                                max_term_tokens=2, require_existing_term=True)

    stored = stored_terms(index_path)
    assert "bitcoin" in stored
    assert "oxyphenbutazone" not in stored
    assert "bitcoin oxyphenbutazone" not in stored


def test_store_wiki_results_never_raises(index_path):
    assert store_wiki_results("query", [wiki_doc("Bitcoin")], "/no/such/index") == 0


# ---------------------------------------------------------------------------
# Automatically stored wiki results are not curation
# ---------------------------------------------------------------------------

def test_stored_wiki_results_are_not_treated_as_curated(index_path):
    # A one-word query's stored results land under exactly the curation term. Treating a
    # non-None state as curation would pin them above everything with ranking skipped.
    term = "bitcoin"
    stored = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin", "money", 3.0,
                      term, state=DocumentState.FROM_WIKI)
    curated = Document("Bitcoin org", "https://bitcoin.org/", "money", 3.0, term,
                       state=DocumentState.FROM_WIKI_APPROVED)

    tiny_index = MagicMock()
    tiny_index.retrieve.return_value = [stored, curated]
    completer = MagicMock()
    completer.complete.return_value = []

    ranker = HeuristicRanker(tiny_index, completer)
    results, _, _ = ranker.get_results(term, [], use_external_search=False)

    assert results[0].url == curated.url, "curated results still come first"
    # The automatically stored one is present, but as an ordinary ranked candidate.
    assert stored.url in {result.url for result in results}


# ---------------------------------------------------------------------------
# LTRRanker wiring
# ---------------------------------------------------------------------------

class _RecordingRanker(LTRRanker):
    """LTRRanker with the Wikipedia fetch and the index write recorded, not performed."""

    def __init__(self, index_results):
        tiny_index = MagicMock()
        tiny_index.retrieve.return_value = []
        completer = MagicMock()
        completer.complete.return_value = []
        self.fetched = []
        self.written = []
        super().__init__(
            tiny_index, completer, model=MagicMock(),
            wiki_fetcher=self._fetch, wiki_store=self._store, wiki_index_path="/unused",
        )
        self.index_results = index_results

    def _fetch(self, query, num_results):
        self.fetched.append(query)
        return [wiki_doc("Bitcoin")]

    def _store(self, query, documents, index_path):
        self.written.append((query, documents))
        return len(documents)

    def order_results(self, terms, results, is_complete):
        return results


def test_cache_disabled_always_calls_wikipedia_and_writes_nothing():
    ranker = _RecordingRanker([wiki_doc("Bitcoin")] * 5)
    with override_settings(WIKI_INDEX_CACHE_ENABLED=False):
        ranker.external_search("bitcoin", ranker.index_results)

    assert ranker.fetched == ["bitcoin"]
    assert ranker.written == []


def test_gate_suppresses_the_call_when_the_index_already_has_wiki_results():
    ranker = _RecordingRanker([wiki_doc("Bitcoin"), wiki_doc("Blockchain")])
    with override_settings(WIKI_INDEX_CACHE_ENABLED=True, WIKI_INDEX_GATE="ranked_top_n",
                           WIKI_INDEX_GATE_THRESHOLD=2, WIKI_INDEX_GATE_TOP_N=10):
        results = ranker.external_search("bitcoin", ranker.index_results)

    assert ranker.fetched == []
    assert results == []


def test_a_miss_fetches_and_writes_the_results_back():
    ranker = _RecordingRanker([])
    with override_settings(WIKI_INDEX_CACHE_ENABLED=True, WIKI_INDEX_GATE="ranked_top_n",
                           WIKI_INDEX_GATE_THRESHOLD=2, WIKI_INDEX_GATE_TOP_N=10):
        results = ranker.external_search("bitcoin", ranker.index_results)

    assert ranker.fetched == ["bitcoin"]
    assert [query for query, _ in ranker.written] == ["bitcoin"]
    assert len(results) == 1


def test_include_wiki_off_never_calls_or_writes():
    ranker = _RecordingRanker([])
    ranker.include_wiki = False
    with override_settings(WIKI_INDEX_CACHE_ENABLED=True):
        assert ranker.external_search("bitcoin", ranker.index_results) == []
    assert ranker.fetched == []
    assert ranker.written == []


# ---------------------------------------------------------------------------
# What the stored copies look like
# ---------------------------------------------------------------------------

def stored_documents(index_path: str) -> list[Document]:
    documents = []
    with TinyIndex(Document, index_path, 'r') as indexer:
        for page in range(NUM_PAGES):
            documents += indexer.get_page(page)
    return documents


def test_stored_results_keep_the_from_wiki_state(index_path):
    # Without the state the result cannot be shown with source `wikipedia`, and the
    # from_wiki_only gate - which counts exactly these - can never fire.
    document = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin", "", 3.0,
                        "bitcoin", state=DocumentState.FROM_WIKI)

    store_wiki_results("bitcoin", [document], index_path)

    stored = stored_documents(index_path)
    assert stored
    assert all(d.state == DocumentState.FROM_WIKI for d in stored)


def test_term_exists_override_decides_which_terms_may_be_written(index_path):
    # The corpus a caller means need not be the index it writes to: the evaluation reads
    # through an overlay and has to answer for both halves.
    document = Document("Bitcoin oxyphenbutazone", "https://en.wikipedia.org/wiki/Bitcoin",
                        "", 3.0, None, state=DocumentState.FROM_WIKI)

    with override_settings(WIKI_INDEX_REQUIRE_EXISTING_TERM=True, WIKI_INDEX_MAX_TERM_TOKENS=2):
        store_wiki_results("bitcoin oxyphenbutazone", [document], index_path,
                           term_exists=lambda term: term == "oxyphenbutazone")

    assert stored_terms(index_path) == {"oxyphenbutazone"}


# ---------------------------------------------------------------------------
# Per-term score-profile gates
# ---------------------------------------------------------------------------

def constant_score(value: float):
    return lambda documents: [value] * len(documents)


def stored_under(term: str, title: str = "Bitcoin"):
    document = wiki_doc(title)
    return Document(document.title, document.url, document.extract, 3.0, term,
                    state=DocumentState.FROM_WIKI)


def test_query_terms_are_unigrams_and_bigrams():
    assert query_terms("bitcoin blockchain ledger") == [
        "bitcoin", "blockchain", "ledger", "bitcoin blockchain", "blockchain ledger"]


def test_terms_with_no_wiki_results_count_as_zero():
    # A term the index cannot answer is the whole signal a breadth gate needs, so it has to
    # stay in the denominator rather than being dropped from the average.
    by_term = wiki_documents_by_term("bitcoin blockchain", [stored_under("bitcoin")])

    assert set(by_term) == {"bitcoin", "blockchain", "bitcoin blockchain"}
    assert per_term_best(by_term, constant_score(1.0)) == [1.0, 0.0, 0.0]


def test_wiki_documents_by_term_ignores_crawled_wiki_pages_by_default():
    crawled = Document("Bitcoin", "https://en.wikipedia.org/wiki/Bitcoin", "e", 1.0, "bitcoin")
    by_term = wiki_documents_by_term("bitcoin", [crawled])
    assert by_term["bitcoin"] == []
    assert wiki_documents_by_term("bitcoin", [crawled], stored_only=False)["bitcoin"] == [crawled]


def test_mean_max_gate_averages_over_every_query_term():
    # One of three terms answered at score 0.9 -> mean 0.3.
    documents = [stored_under("bitcoin")]
    assert have_enough_wiki_results("bitcoin blockchain", documents, identity_rank,
                                    constant_score(0.9), gate="mean_max_ltr", threshold=0.3)
    assert not have_enough_wiki_results("bitcoin blockchain", documents, identity_rank,
                                        constant_score(0.9), gate="mean_max_ltr", threshold=0.31)


def test_min_max_gate_needs_every_term_answered():
    both = [stored_under("bitcoin"), stored_under("blockchain", "Blockchain"),
            stored_under("bitcoin blockchain", "Bitcoin blockchain")]
    assert have_enough_wiki_results("bitcoin blockchain", both, identity_rank,
                                    constant_score(0.5), gate="min_max_ltr", threshold=0.5)
    assert not have_enough_wiki_results("bitcoin blockchain", both[:1], identity_rank,
                                        constant_score(0.5), gate="min_max_ltr", threshold=0.5)


def test_term_coverage_gate_is_a_fraction_of_terms():
    documents = [stored_under("bitcoin")]
    assert have_enough_wiki_results("bitcoin blockchain", documents, identity_rank,
                                    gate="term_coverage", threshold=1 / 3)
    assert not have_enough_wiki_results("bitcoin blockchain", documents, identity_rank,
                                        gate="term_coverage", threshold=0.5)


def test_value_gates_do_not_rank():
    # Ranking is what the counting gates pay for; a per-term profile does not need an order.
    rank = MagicMock(side_effect=identity_rank)
    have_enough_wiki_results("bitcoin", [stored_under("bitcoin")], rank,
                             constant_score(1.0), gate="mean_max_ltr", threshold=0.5)
    rank.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrent writes from the request path
# ---------------------------------------------------------------------------

def _hold_page_lock(index_path: str, page: int, acquired, release):
    """Child process: take the page lock, say so, and hold it until told to let go."""
    with TinyIndex(Document, index_path, 'w') as indexer:
        with indexer.locked_page(page):
            acquired.set()
            release.wait(timeout=30)


def _try_lock_in_child(index_path: str, page: int, queue):
    """Child process: report whether the page lock is free."""
    from mwmbl.tinysearchengine.indexer import F_OFD_SETLKW, _FLOCK_STRUCT
    import struct
    with TinyIndex(Document, index_path, 'w') as indexer:
        start = page * indexer.page_size + METADATA_SIZE
        try:
            fcntl.fcntl(indexer.index_file.fileno(), F_OFD_SETLKW - 1,  # F_OFD_SETLK
                        struct.pack(_FLOCK_STRUCT, fcntl.F_WRLCK, os.SEEK_SET,
                                    start, indexer.page_size, 0))
            queue.put(True)
        except OSError:
            queue.put(False)


def test_locked_page_excludes_another_process(index_path):
    """The page lock must exclude across processes, which is the only case that matters.

    Storing a page is read-merge-write with no atomicity. _write_page copies ~4 KB into the
    mmap; a reader catching it half-written gets a ZstdError, which _get_page_tuples turns
    into an empty page - so an unlocked writer can read empty, merge, and store its own
    documents over everything else on that page.

    This asserts the exclusion property directly rather than trying to win the race: the
    window is a single memcpy, so a racing test passes with the lock removed and guards
    nothing.
    """
    import multiprocessing

    context = multiprocessing.get_context("fork")
    with TinyIndex(Document, index_path, 'r') as indexer:
        page = indexer.get_key_page_index("zebra")

    def child_can_lock():
        queue = context.Queue()
        child = context.Process(target=_try_lock_in_child, args=(index_path, page, queue))
        child.start()
        child.join(timeout=30)
        return queue.get(timeout=5)

    with TinyIndex(Document, index_path, 'w') as indexer:
        with indexer.locked_page(page):
            assert not child_can_lock(), "another process took a lock we were holding"

            # An ordinary POSIX record lock is owned by the *process* and is dropped the
            # moment any fd on the file is closed - and index_results_against_query opens
            # and closes a read handle on every store, so in a threaded worker that would
            # silently release the lock mid-write. OFD locks are owned by the open file
            # description and survive it.
            with TinyIndex(Document, index_path, 'r'):
                pass
            assert not child_can_lock(), \
                "closing an unrelated fd released the lock - this needs an OFD lock"

        assert child_can_lock(), "the lock was not released"


def test_locked_page_does_not_serialise_different_pages(index_path):
    """Per-page granularity: writers of unrelated pages must not block each other."""
    import multiprocessing

    context = multiprocessing.get_context("fork")
    with TinyIndex(Document, index_path, 'r') as indexer:
        page = indexer.get_key_page_index("zebra")
        other_page = (page + 1) % indexer.num_pages

    with TinyIndex(Document, index_path, 'w') as indexer:
        with indexer.locked_page(page):
            queue = context.Queue()
            child = context.Process(target=_try_lock_in_child,
                                    args=(index_path, other_page, queue))
            child.start()
            child.join(timeout=30)
            assert queue.get(timeout=5), "a different page was blocked"


def test_writes_continue_when_page_locking_is_unsupported(index_path, monkeypatch):
    """A filesystem without record locks must not break indexing.

    Every path through index_pages ran with no lock at all before this existed, so an
    environment that cannot lock should degrade to that, not fail.
    """
    from mwmbl.tinysearchengine import indexer as indexer_module

    monkeypatch.setattr(indexer_module, "_page_locking_supported", True)
    monkeypatch.setattr(indexer_module, "_set_page_lock",
                        lambda *args: (_ for _ in ()).throw(OSError("no locks available")))

    document = Document("Zebra", "https://en.wikipedia.org/wiki/Zebra", "", 3.0, None,
                        state=DocumentState.FROM_WIKI)
    assert index_results_against_query([document], "zebra", index_path,
                                       state=DocumentState.FROM_WIKI) == 1

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert {d.url for d in indexer.retrieve("zebra")} == {document.url}
    assert indexer_module._page_locking_supported is False


def test_locked_page_needs_write_mode(index_path):
    with TinyIndex(Document, index_path, 'r') as indexer:
        with pytest.raises(UnsupportedOperation):
            with indexer.locked_page(0):
                pass


def _store_repeatedly(index_path: str, term: str, url_prefix: str, count: int):
    """Child-process worker: store documents under `term`, over and over."""
    for i in range(count):
        # The title has to contain the term, or the containment rule stores nothing.
        index_results_against_query(
            [Document(f"Zebra {url_prefix} {i}", f"https://example.com/{url_prefix}/{i}", "",
                      3.0, None, state=DocumentState.FROM_WIKI)],
            term, index_path, state=DocumentState.FROM_WIKI)


def test_concurrent_writers_leave_the_page_readable(index_path):
    """Smoke test: four processes hammering one page leave it decodable, not empty."""
    import multiprocessing

    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_store_repeatedly,
                               args=(index_path, "zebra", f"w{n}", 30))
               for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0

    with TinyIndex(Document, index_path, 'r') as indexer:
        page = indexer.get_page(indexer.get_key_page_index("zebra"))

    assert page, "the page was left empty"
    assert all(document.title and document.url for document in page)
