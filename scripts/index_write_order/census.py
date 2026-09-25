"""Count what a rebuild of the index would have to re-file: documents, pages, unique URLs.

Read-only. Scans a sample of the index's pages (every STRIDE-th) and estimates, for the
whole index:

- entries: (term, document) pairs stored;
- unique URLs: from the URLs whose hash falls in a 1/URL_SAMPLE slice, counted exactly
  across the sampled pages and scaled up. A URL is stored under many terms, on many pages,
  so sampling pages undercounts URLs stored only a few times - see the note it prints;
- full pages and time per page read, to size the scan itself.

    DJANGO_SETTINGS_MODULE=mwmbl.settings_prod uv run python scripts/index_write_order/census.py [STRIDE]
"""

import sys
import time
from pathlib import Path

import django

django.setup()

import mmh3
from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document, PageError, TinyIndex

URL_SAMPLE = 64


def main():
    stride = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    entries = pages_read = empty = unreadable = 0
    per_page_counts = []
    sampled_urls = set()
    start = time.monotonic()
    with TinyIndex(item_factory=Document, index_path=str(path)) as index:
        for page_index in range(0, index.num_pages, stride):
            try:
                documents = index.get_page(page_index)
            except PageError:
                unreadable += 1
                continue
            pages_read += 1
            entries += len(documents)
            per_page_counts.append(len(documents))
            empty += not documents
            for document in documents:
                if mmh3.hash(document.url, signed=False) % URL_SAMPLE == 0:
                    sampled_urls.add(document.url)
        num_pages = index.num_pages
    elapsed = time.monotonic() - start
    scale = num_pages / pages_read
    per_page_counts.sort()
    print(f"pages read: {pages_read} of {num_pages} ({unreadable} unreadable), {elapsed:.0f}s")
    print(f"time per page read: {elapsed / pages_read * 1e6:.0f} us")
    print(f"empty pages: {empty / pages_read:.1%}")
    print(f"documents per page: median {per_page_counts[len(per_page_counts) // 2]}, mean {entries / pages_read:.1f}")
    print(f"estimated (term, document) entries: {entries * scale:,.0f}")
    print(f"unique URLs on the pages read: {len(sampled_urls) * URL_SAMPLE:,.0f}")
    if stride == 1:
        print("that is the whole index: exact up to the URL hash sample")
    else:
        print("sampled pages: this is a lower bound on unique URLs; run with a smaller STRIDE to tighten it")


if __name__ == "__main__":
    main()
