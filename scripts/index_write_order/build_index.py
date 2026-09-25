"""Index the A/B corpus into a fresh index with the write-time ranker named by INDEX_PAGE_RANKER.

Only the eval queries' lookup terms are written (build_corpus.py lists them per document),
so the index is small, but each of those pages sees the same competition it would in a
full index. The corpus goes through index_pages in chunks, the way crawl batches arrive.

    INDEX_PAGE_RANKER=ltr DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        PYTHONPATH=. uv run python scripts/index_write_order/build_index.py
"""

import gzip
import json
import time
from collections import defaultdict
from pathlib import Path

import django

django.setup()

from django.conf import settings

from mwmbl.indexer.index_batches import filter_blacklisted_documents, index_pages
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex, cleaned_document

OUT_DIR = Path("devdata/index_write_order")
# About 1,100 lookup terms: collisions are rare, and identical in both arms.
NUM_PAGES = 65536
CHUNK_SIZE = 100_000


def index_path() -> Path:
    return OUT_DIR / f"{settings.INDEX_PAGE_RANKER}.tinysearch"


def read_chunks():
    chunk = []
    with gzip.open(OUT_DIR / "corpus.jsonl.gz", "rt") as file:
        for line in file:
            chunk.append(json.loads(line))
            if len(chunk) == CHUNK_SIZE:
                yield chunk
                chunk = []
    yield chunk


def write_chunk(path: Path, index: TinyIndex, records: list[dict]) -> int:
    documents = [
        Document(record["title"], record["url"], record["extract"], last_crawled=record.get("last_crawled"))
        for record in records
    ]
    terms_by_url = defaultdict(set)
    for record in records:
        terms_by_url[record["url"]].update(record["terms"])
    kept = [cleaned_document(document) for document in filter_blacklisted_documents(documents)]
    page_documents = defaultdict(list)
    for document in kept:
        for term in terms_by_url[document.url]:
            page_documents[index.get_key_page_index(term)].append(
                Document(document.title, document.url, document.extract, term=term, last_crawled=document.last_crawled)
            )
    index_pages(str(path), page_documents)
    return len(kept)


def main():
    path = index_path()
    path.unlink(missing_ok=True)
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    start = time.monotonic()
    num_documents = 0
    with TinyIndex(item_factory=Document, index_path=str(path)) as index:
        for i, records in enumerate(read_chunks()):
            num_documents += write_chunk(path, index, records)
            print(f"chunk {i}: {num_documents} documents, {time.monotonic() - start:.0f}s", flush=True)
    elapsed = time.monotonic() - start
    (OUT_DIR / f"{settings.INDEX_PAGE_RANKER}.timing.json").write_text(
        json.dumps({"documents": num_documents, "seconds": elapsed})
    )
    print(f"{settings.INDEX_PAGE_RANKER}: {num_documents} documents in {elapsed:.0f}s -> {path}")


if __name__ == "__main__":
    main()
