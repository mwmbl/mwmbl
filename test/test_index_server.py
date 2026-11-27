"""The Rust index server, started as the server will start it, through mwmbl_rank."""

import multiprocessing
import os
import signal
import time

import httpx
import pytest

from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex
from mwmbl_rank import serve_index


def _serve(socket_path: str, index_path: str):
    serve_index(socket_path, [("main", index_path)], 2)


@pytest.fixture
def index_server(tmp_path):
    index_path = str(tmp_path / "index.tinysearch")
    socket_path = str(tmp_path / "index.sock")
    TinyIndex.create(Document, index_path, num_pages=64, page_size=4096)
    process = multiprocessing.get_context("spawn").Process(target=_serve, args=(socket_path, index_path))
    process.start()
    client = httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), base_url="http://index-server")
    deadline = time.monotonic() + 30
    while not os.path.exists(socket_path):
        assert process.is_alive() and time.monotonic() < deadline, "The index server did not start"
        time.sleep(0.05)
    yield client, index_path
    client.close()
    os.kill(process.pid, signal.SIGTERM)
    process.join(10)
    assert process.exitcode == 0


def test_python_reads_the_pages_the_server_writes(index_server):
    client, index_path = index_server
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
