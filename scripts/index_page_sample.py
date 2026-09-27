"""Sample random pages of the production index and report how efficiently they are used.

Reads pages through the unauthenticated GET /api/v1/search/page/{n} endpoint, caches them
in devdata/, and prints the tables in docs/index-page-sample.md. Needs no API keys:

    uv run scripts/index_page_sample.py [--pages 1000] [--refetch]
"""

import argparse
import json
import random
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

import mmh3
import requests
import zstandard

from mwmbl.indexer.index import get_index_tokens, prepare_url_for_tokenizing
from mwmbl.tokenizer import tokenize

API = "https://api.mwmbl.org/api/v1/search/page"
NUM_PAGES = 102_400_000  # settings_prod.NUM_PAGES
PAGE_SIZE = 4096
# The level mwmbl_rank compresses pages with - see COMPRESSION_LEVEL in mwmbl_rank/src/index.rs.
COMPRESSION_LEVEL = 3
SEED = 454
CACHE_PATH = Path("devdata/index_page_sample/pages.json")

ERROR_TITLE = re.compile(
    r"^(just a moment|40\d|50\d|access denied|attention required|error|forbidden|page not found|not found|"
    r"service unavailable|too many requests|robot|captcha|security check|request rejected)",
    re.I,
)
UNQUERYABLE_TERM = re.compile(r"[?=&]|[0-9a-f]{12,}|://")

session = requests.Session()
compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)


def fetch_page(page_index: int) -> list:
    response = session.get(f"{API}/{page_index}", timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_pages(page_indexes: list[int]) -> dict[int, list]:
    with ThreadPoolExecutor(16) as executor:
        return dict(zip(page_indexes, executor.map(fetch_page, page_indexes)))


def serialise(value) -> bytes:
    """JSON as serde_json writes it on a page: compact, non-ASCII left as is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def compressed_size(documents: list, compressor=compressor) -> int:
    return len(compressor.compress(serialise(documents)))


def num_that_fit(documents: list, compressor=compressor) -> int:
    """How many of documents a page stores: the longest prefix that compresses to fit."""
    low, high, best = 0, len(documents), 0
    while low <= high:
        count = (low + high) // 2
        if compressed_size(documents[:count], compressor) <= PAGE_SIZE:
            best, low = count, count + 1
        else:
            high = count - 1
    return best


def percentiles(values: list) -> str:
    ordered = sorted(values)
    return ", ".join(f"p{p}={ordered[len(ordered) * p // 100]}" for p in (10, 25, 50, 75, 90))


def mean_capacity(documents: list, transform=lambda d: d, compressor=compressor) -> float:
    """Mean number of random documents that fit on one page, over 40 draws."""
    capacities = []
    for seed in range(40):
        draw = [transform(list(d)) for d in random.Random(seed).sample(documents, 150)]
        capacities.append(num_that_fit(draw, compressor))
    return statistics.mean(capacities)


def document_terms(document: list) -> set[str]:
    """The terms the crawl indexer files a document under - see tokenize_document."""
    title, url, extract = document[:3]
    url_tokens = tokenize(prepare_url_for_tokenizing(unquote(url)))
    return get_index_tokens(tokenize(title)) | get_index_tokens(url_tokens) | get_index_tokens(tokenize(extract))


def report_fill(pages: dict[int, list]):
    sizes = [compressed_size(page) for page in pages.values()]
    counts = [len(page) for page in pages.values()]
    print(f"Pages: {len(pages)}, documents: {sum(counts)}")
    print(f"Documents per page: {percentiles(counts)}")
    print(f"Compressed bytes per page: {percentiles(sizes)}")
    print(f"Mean fill: {statistics.mean(sizes) / PAGE_SIZE:.0%}; >=3900 bytes (full): {sum(s >= 3900 for s in sizes)}")
    terms_per_page = [len({d[4] for d in page}) for page in pages.values()]
    print(f"Distinct terms per page: {percentiles(terms_per_page)}")

    wrong_page = sum(mmh3.hash(d[4], signed=False) % NUM_PAGES != i for i, page in pages.items() for d in page)
    print(f"Documents whose term does not hash to their page: {wrong_page}")

    shuffled = list(pages.values())
    random.Random(1).shuffle(shuffled)
    pairs = [a + b for a, b in zip(shuffled[::2], shuffled[1::2])]
    overflowing = sum(compressed_size(pair) > PAGE_SIZE for pair in pairs)
    lost = sum(len(pair) - num_that_fit(pair) for pair in pairs)
    total = sum(len(pair) for pair in pairs)
    print(
        f"Halving NUM_PAGES: {overflowing / len(pairs):.0%} of merged pages overflow, losing {lost / total:.0%} of docs"
    )


def report_bytes(documents: list):
    fields = ["title", "url", "extract", "score", "term", "state", "user_ids", "last_crawled", "source"]
    field_bytes = Counter()
    for document in documents:
        for name, value in zip(fields, document):
            field_bytes[name] += len(serialise(value)) + 1
    total = sum(field_bytes.values())
    print("Share of uncompressed bytes:", {name: f"{b / total:.1%}" for name, b in field_bytes.most_common(5)})

    n = len(documents)
    print(f"Empty extract: {sum(not d[2] for d in documents) / n:.0%}")
    print(f"Null score: {sum(len(d) < 4 or d[3] is None for d in documents) / n:.0%}")
    print(f"Bigram terms: {sum(' ' in d[4] for d in documents) / n:.0%}")
    print(
        f"Unqueryable-looking terms (query strings, hashes, >40 chars): "
        f"{sum(bool(UNQUERYABLE_TERM.search(d[4])) or len(d[4]) > 40 for d in documents) / n:.0%}"
    )
    print(
        f"Empty or error-page title: {sum(not d[0].strip() or bool(ERROR_TITLE.search(d[0].strip())) for d in documents) / n:.1%}"
    )
    github_boilerplate = sum(d[2].lstrip().startswith(("Saved searches", "You signed in")) for d in documents)
    print(f"GitHub chrome as extract: {github_boilerplate / n:.1%}")
    hosts = Counter(urlparse(d[1]).netloc.lower().removeprefix("www.") for d in documents)
    print(f"Distinct hosts: {len(hosts)}; top: {[(h, f'{c / n:.1%}') for h, c in hosts.most_common(5)]}")


def report_compression(pages: dict[int, list], documents: list):
    page_list = list(pages.values())
    train, test = page_list[: len(page_list) // 2], page_list[len(page_list) // 2 :]
    raw = sum(len(serialise(page)) for page in test)
    for level in (3, 19):
        level_compressor = zstandard.ZstdCompressor(level=level)
        print(f"zstd level {level}: ratio {raw / sum(compressed_size(p, level_compressor) for p in test):.2f}")
    dictionary = zstandard.train_dictionary(112_640, [serialise(d) for page in train for d in page])
    dict_compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL, dict_data=dictionary)
    print(f"zstd level 3 + 110 KB dictionary: ratio {raw / sum(compressed_size(p, dict_compressor) for p in test):.2f}")

    test_documents = [d for page in test for d in page]
    print(
        f"Documents per full page: plain {mean_capacity(test_documents):.1f}, "
        f"with dictionary {mean_capacity(test_documents, compressor=dict_compressor):.1f}"
    )

    def round_score(d):
        if len(d) > 3 and isinstance(d[3], float):
            d[3] = round(d[3], 3)
        return d

    def strip_scheme(d):
        d[1] = re.sub(r"^https?://(www\.)?", "", d[1])
        return d

    for name, transform in [("score to 3 d.p.", round_score), ("strip scheme and www.", strip_scheme)]:
        print(f"Documents per full page, {name}: {mean_capacity(documents, transform):.1f}")


def report_replication(documents: list, sample_size: int = 80):
    """How many copies of a document the index holds, one per term it is filed under."""
    sample = random.Random(7).sample(documents, sample_size)
    terms = {d[1]: document_terms(d) for d in sample}
    page_indexes = sorted({mmh3.hash(t, signed=False) % NUM_PAGES for ts in terms.values() for t in ts})
    pages = fetch_pages(page_indexes)
    copies = []
    for document in sample:
        stored_under = {
            d[4]
            for t in terms[document[1]]
            for d in pages[mmh3.hash(t, signed=False) % NUM_PAGES]
            if d[1] == document[1]
        }
        copies.append(len(stored_under & terms[document[1]]))
    print(
        f"Candidate terms per document: {statistics.mean(len(t) for t in terms.values()):.1f}; "
        f"copies found: mean {statistics.mean(copies):.1f}, {percentiles(copies)}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()

    if args.refetch or not CACHE_PATH.exists():
        page_indexes = random.Random(SEED).sample(range(NUM_PAGES), args.pages)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(fetch_pages(page_indexes)))
    pages = {int(i): page for i, page in json.loads(CACHE_PATH.read_text()).items()}
    documents = [d for page in pages.values() for d in page]

    report_fill(pages)
    report_bytes(documents)
    report_compression(pages, documents)
    report_replication(documents)


if __name__ == "__main__":
    main()
