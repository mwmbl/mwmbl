"""
A client of the Rust index server (rust/index_server), the only writer to the index files
once it is turned on. It listens on a Unix socket in this container - see mwmbl.main.
"""

import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from logging import getLogger
from typing import Any, Callable, Iterable, Optional, TypeVar

import httpx

from mwmbl.indexer.agreement import Agreement
from mwmbl.indexer.index_batches import IndexedDocuments, IndexedURLs
from mwmbl.tinysearchengine.indexer import Document

logger = getLogger(__name__)

# The server's body limit, with room for the envelope around the items.
MAX_BODY_BYTES = 32 * 1024 * 1024 - 64 * 1024

# During a deploy the new container's server queues requests until the old one releases
# the index lock. Dokku keeps the old container for wait-to-retire (60 s by default) after
# the new one is live, and then stops it within INDEX_SERVER_STOP_SECONDS, so a request can
# wait about 60 + 30 s for its answer. Both bounds are twice that.
TIMEOUT_SECONDS = 180
RETRY_SECONDS = 180
FIRST_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 10

T = TypeVar("T")


class IndexServerUnavailable(Exception):
    """The server did not answer within RETRY_SECONDS."""


def _document_json(document: Document) -> dict[str, Any]:
    return {key: value for key, value in asdict(document).items() if value is not None}


def _document(item: dict[str, Any]) -> Document:
    """Reads drop the elements a newer build added, as TinyIndex.get_page does."""
    fields = {key: value for key, value in item.items() if key != "extra"}
    return Document(**fields)


def _chunks(items: Iterable[T], size: Callable[[T], int]) -> Iterable[list[T]]:
    chunk = []
    chunk_bytes = 0
    for item in items:
        item_bytes = size(item)
        if chunk and chunk_bytes + item_bytes > MAX_BODY_BYTES:
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(item)
        chunk_bytes += item_bytes
    if chunk:
        yield chunk


def _json_size(value: Any) -> int:
    return len(json.dumps(value).encode()) + 1


class IndexClient:
    def __init__(self, socket_path: str):
        transport = httpx.HTTPTransport(uds=socket_path)
        self.client = httpx.Client(transport=transport, base_url="http://index-server", timeout=TIMEOUT_SECONDS)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Retries while the server restarts or has yet to take its lock, then raises.

        A connection dropped mid-request may follow a write the server applied, and the
        retry applies it again. Merging the same documents twice leaves the pages as once,
        but the second answer counts their URLs as held, so new_urls can be undercounted.
        """
        deadline = time.monotonic() + RETRY_SECONDS
        backoff = FIRST_BACKOFF_SECONDS
        while True:
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code != httpx.codes.SERVICE_UNAVAILABLE:
                    response.raise_for_status()
                    return response
                failure = response.text
            except (httpx.ConnectError, httpx.RemoteProtocolError) as error:
                failure = repr(error)
            if time.monotonic() + backoff > deadline:
                raise IndexServerUnavailable(f"{method} {path}: {failure}")
            logger.warning("Index server unavailable for %s %s, retrying: %s", method, path, failure)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def index_documents(self, index: str, documents: list[Document]) -> IndexedDocuments:
        new_page_doc_counts = Counter()
        agreements = []
        stored, new = set(), set()
        for chunk in _chunks(map(_document_json, documents), _json_size):
            result = self._request("POST", f"/v1/{index}/documents", json={"documents": chunk}).json()
            new_page_doc_counts.update(result["new_page_doc_counts"])
            agreements += [Agreement(**agreement) for agreement in result["agreements"]]
            stored |= set(result["stored_urls"])
            new |= set(result["new_urls"])
        return IndexedDocuments(datetime.now(timezone.utc), new_page_doc_counts, agreements, IndexedURLs(stored, new))

    def index_pages(
        self, index: str, page_documents: dict[int, list[Document]], mark_synced_term: Optional[str] = None
    ) -> Counter:
        pages = [
            (page, [_document_json(document) for document in documents]) for page, documents in page_documents.items()
        ]
        new_page_doc_counts = Counter()
        for chunk in _chunks(pages, _json_size):
            body = {"pages": dict(chunk), "mark_synced_term": mark_synced_term}
            new_page_doc_counts.update(
                self._request("POST", f"/v1/{index}/pages", json=body).json()["new_page_doc_counts"]
            )
        return new_page_doc_counts

    def retrieve(self, index: str, terms: list[str]) -> dict[str, list[Document]]:
        results = self._request("POST", f"/v1/{index}/retrieve", json={"terms": terms}).json()["results"]
        return {term: [_document(item) for item in items] for term, items in results.items()}

    def read_page(self, index: str, page: int) -> tuple[list[dict[str, Any]], str]:
        """The page's documents as the server sends them, `extra` included, and its version."""
        result = self._request("GET", f"/v1/{index}/page/{page}").json()
        return result["documents"], result["version"]

    def write_page(self, index: str, page: int, documents: list[dict[str, Any]], if_version: str) -> Optional[int]:
        """How many of the documents fit, or None when the page is no longer at `if_version`."""
        try:
            response = self._request(
                "PUT", f"/v1/{index}/page/{page}", params={"if_version": if_version}, json={"documents": documents}
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == httpx.codes.CONFLICT:
                return None
            raise
        return response.json()["num_stored"]
