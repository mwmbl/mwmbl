"""Time the unit of work of rebuilding the index from itself, per document, for each ranker.

A rebuild re-files every unique document under all of its terms (tokenize_document) and
writes each term's page with the configured ranker, so its cost is documents x terms per
document x (ranking + page read/write). This indexes N real crawl documents under all
their terms into a scratch index big enough that pages rarely collide, and reports the
time split, with INDEX_PAGE_RANKER set per run.

    INDEX_PAGE_RANKER=ltr DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        PYTHONPATH=. uv run python scripts/index_write_order/benchmark_rebuild.py 20000
"""

import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import django

django.setup()

from django.conf import settings

from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.index_batches import index_pages
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex

BATCH_DIR = Path("devdata/batches")
OUT_DIR = Path("devdata/index_write_order")
NUM_PAGES = 2_000_000


def crawl_documents(limit: int) -> list[Document]:
    documents = []
    for path in sorted(BATCH_DIR.rglob("*.json.gz"))[5000:]:
        try:
            with gzip.open(path) as file:
                items = json.load(file)["items"]
        except (json.JSONDecodeError, EOFError, gzip.BadGzipFile):
            continue
        for item in items:
            content = item.get("content")
            if content and not content.get("links_only") and content.get("title"):
                documents.append(Document(content["title"], item["url"], content.get("extract") or ""))
        if len(documents) >= limit:
            return documents[:limit]
    return documents


def main():
    limit = int(sys.argv[1])
    documents = crawl_documents(limit)
    path = OUT_DIR / f"bench-{settings.INDEX_PAGE_RANKER}.tinysearch"
    path.unlink(missing_ok=True)
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=PAGE_SIZE)

    start = time.monotonic()
    page_documents = defaultdict(list)
    num_pairs = 0
    with TinyIndex(item_factory=Document, index_path=str(path)) as index:
        for document in documents:
            for term in tokenize_document(document.url, document.title, document.extract, 0.0).tokens:
                page_documents[index.get_key_page_index(term)].append(
                    Document(document.title, document.url, document.extract, term=term)
                )
                num_pairs += 1
    tokenized = time.monotonic()
    index_pages(str(path), page_documents)
    written = time.monotonic()
    path.unlink()

    result = {
        "ranker": settings.INDEX_PAGE_RANKER,
        "documents": len(documents),
        "pairs": num_pairs,
        "pages": len(page_documents),
        "tokenize_us_per_doc": (tokenized - start) / len(documents) * 1e6,
        "write_us_per_pair": (written - tokenized) / num_pairs * 1e6,
        "total_ms_per_doc": (written - start) / len(documents) * 1e3,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
