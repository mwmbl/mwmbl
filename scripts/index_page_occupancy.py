"""How full are the index's pages?

Pages are a fixed size and silently truncate their tail, so anything added to a page costs
whatever was at the end of it. That makes page occupancy the number that decides how much a
feature like the Wikipedia cache (mwmbl.indexer.wiki_cache) actually costs: on a page with
room to spare it costs nothing, on a saturated page it evicts about as much as it adds.

Run it against prod before turning the general Wikipedia indexing up:

    DJANGO_SETTINGS_MODULE=mwmbl.settings_prod uv run python scripts/index_page_occupancy.py

For reference, the dev index (2560 pages) measures ~35 documents and 4037 of 4096 bytes per
page - every page over 90% full.
"""
import argparse
import json
import os
from random import Random

import django
from zstandard import ZstdDecompressor, ZstdError

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.conf import settings                                       # noqa: E402
from pathlib import Path                                               # noqa: E402
from mwmbl.tinysearchengine.indexer import (                           # noqa: E402
    Document, METADATA_SIZE, TinyIndex,
)


def percentile(values: list[int], fraction: float) -> int:
    return values[min(int(len(values) * fraction), len(values) - 1)]


def summarise(name: str, values: list[int]) -> str:
    values = sorted(values)
    return (f"{name}: mean {sum(values) / len(values):.1f}  median {percentile(values, 0.5)}  "
            f"p90 {percentile(values, 0.9)}  p99 {percentile(values, 0.99)}  max {values[-1]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=2000, help="pages to sample")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    decompressor = ZstdDecompressor()

    with TinyIndex(item_factory=Document, index_path=index_path) as index:
        sample = Random(args.seed).sample(range(index.num_pages),
                                          min(args.sample, index.num_pages))
        doc_counts = []
        bytes_used = []
        unreadable = 0
        for page in sample:
            start = page * index.page_size + METADATA_SIZE
            raw = index.mmap[start:start + index.page_size]
            try:
                items = json.loads(decompressor.decompress(raw).decode('utf8'))
            except (ZstdError, ValueError):
                unreadable += 1
                continue
            doc_counts.append(len(items))
            bytes_used.append(len(raw.rstrip(b'\x00')))

        page_size = index.page_size
        num_pages = index.num_pages

    sampled = len(doc_counts)
    empty = sum(1 for count in doc_counts if count == 0)
    nearly_full = sum(1 for used in bytes_used if used > 0.9 * page_size)

    print(f"{index_path}: {num_pages} pages of {page_size} bytes")
    print(f"sampled {sampled} pages ({unreadable} unreadable)")
    print(summarise("documents per page", doc_counts))
    print(summarise(f"bytes used of {page_size}", bytes_used))
    print(f"empty pages: {100 * empty / sampled:.1f}%")
    print(f"pages over 90% full: {100 * nearly_full / sampled:.1f}%")
    print(f"estimated documents in the index: "
          f"{int(sum(doc_counts) / sampled * num_pages):,}")


if __name__ == "__main__":
    main()
