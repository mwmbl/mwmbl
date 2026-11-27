"""IndexClient against the Rust index server."""

import httpx
import pytest

from mwmbl.indexer import index_client
from mwmbl.indexer.index_client import IndexClient, IndexServerUnavailable
from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex


@pytest.fixture
def client(index_server):
    return IndexClient(index_server.socket_path)


def test_indexed_documents_are_stored_once_and_retrieved(client):
    document = Document("Rust", "https://rust-lang.org/", "rust is a language", user_ids=[1], last_crawled=100)

    first = client.index_documents("main", [document])
    second = client.index_documents("main", [document])
    retrieved = client.retrieve("main", ["rust", "missing"])

    assert first.urls.stored == first.urls.new == {"https://rust-lang.org/"}
    assert second.urls.stored == {"https://rust-lang.org/"} and second.urls.new == set()
    assert first.new_page_doc_counts["rust"] == 1
    assert [(doc.url, doc.term, doc.user_ids) for doc in retrieved["rust"]] == [("https://rust-lang.org/", "rust", [1])]
    assert retrieved["missing"] == []


def test_batches_are_chunked_to_the_body_limit(client, monkeypatch):
    monkeypatch.setattr(index_client, "MAX_BODY_BYTES", 300)
    documents = [Document(f"Page {i}", f"https://example.com/{i}", "an example page") for i in range(20)]

    indexed = client.index_documents("main", documents)

    assert indexed.urls.new == {document.url for document in documents}


def test_index_pages_marks_the_term_synced(client, index_server):
    with TinyIndex(Document, index_server.index_path) as index:
        page = index.get_key_page_index("rust")
    document = Document("Rust", "https://rust-lang.org/", "rust", term="rust")

    counts = client.index_pages("main", {page: [document]}, mark_synced_term="rust")

    assert counts == {}
    [stored] = client.retrieve("main", ["rust"])["rust"]
    assert stored.state == DocumentState.SYNCED_WITH_MAIN_INDEX


def test_a_page_write_keeps_extra_and_conflicts_on_a_stale_version(client):
    documents, version = client.read_page("main", 3)
    written = [{"title": "Rust", "url": "https://rust-lang.org/", "extract": "", "term": "rust", "extra": [1, "x"]}]

    assert documents == []
    assert client.write_page("main", 3, written, version) == 1
    assert client.write_page("main", 3, [], version) is None
    [stored] = client.read_page("main", 3)[0]
    assert (stored["url"], stored["extra"]) == ("https://rust-lang.org/", [1, "x"])


def test_requests_are_retried_while_the_server_is_unavailable(monkeypatch):
    monkeypatch.setattr(index_client, "FIRST_BACKOFF_SECONDS", 0.01)
    responses = iter([httpx.Response(503, text="no lock yet"), httpx.Response(200, json={"results": {"rust": []}})])
    client = IndexClient("/nonexistent.sock")
    client.client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)), base_url="http://x")

    assert client.retrieve("main", ["rust"]) == {"rust": []}


def test_requests_fail_once_the_retries_run_out(tmp_path, monkeypatch):
    monkeypatch.setattr(index_client, "RETRY_SECONDS", 0.1)
    monkeypatch.setattr(index_client, "FIRST_BACKOFF_SECONDS", 0.01)

    with pytest.raises(IndexServerUnavailable):
        IndexClient(str(tmp_path / "missing.sock")).retrieve("main", ["rust"])
