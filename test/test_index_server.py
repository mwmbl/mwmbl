"""The Rust index server, started as the server will start it, through mwmbl_rank."""

import httpx
import pytest

from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex


@pytest.fixture
def server(index_server):
    transport = httpx.HTTPTransport(uds=index_server.socket_path)
    with httpx.Client(transport=transport, base_url="http://index-server") as client:
        yield client, index_server.index_path


def test_python_reads_the_pages_the_server_writes(server):
    client, index_path = server
    document = {"title": "Rust", "url": "https://rust-lang.org/", "extract": "rust is a language"}
    with TinyIndex(Document, index_path) as index:
        page = index.get_key_page_index("rust")
    curated = {**document, "title": "The Rust Book", "url": "https://doc.rust-lang.org/", "term": "rust", "state": 7}

    indexed = client.post("/v1/main/documents", json={"documents": [document]})
    pages = client.post("/v1/main/pages", json={"pages": {str(page): [curated]}})

    assert indexed.status_code == 200, indexed.text
    assert pages.status_code == 200, pages.text
    with TinyIndex(Document, index_path) as index:
        retrieved = index.retrieve("rust")
    assert {(document.url, document.term, document.state) for document in retrieved} == {
        ("https://rust-lang.org/", "rust", None),
        ("https://doc.rust-lang.org/", "rust", DocumentState.ORGANIC_APPROVED),
    }


def test_a_conditional_write_lands_once_and_python_reads_it(server):
    client, index_path = server
    document = {"title": "Rust", "url": "https://rust-lang.org/", "extract": "rust is a language", "term": "rust"}
    with TinyIndex(Document, index_path) as index:
        page = index.get_key_page_index("rust")
    version = client.get(f"/v1/main/page/{page}").json()["version"]

    written = client.put(f"/v1/main/page/{page}", params={"if_version": version}, json={"documents": [document]})
    stale = client.put(f"/v1/main/page/{page}", params={"if_version": version}, json={"documents": []})

    assert written.json() == {"num_stored": 1}, written.text
    assert stale.status_code == 409
    with TinyIndex(Document, index_path) as index:
        assert [document.url for document in index.retrieve("rust")] == ["https://rust-lang.org/"]
