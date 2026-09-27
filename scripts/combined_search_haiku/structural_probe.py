"""Step 1 of the structural-fixes study: measure the pages that evict good results.

Reads, from the live index:

- every query lookup term's list, and its whole 4 KB page (term_info.json): how full the
  page is, how many documents the term keeps and what share of the page it holds;
- the evicted good results (targets.json): Brave results graded 2-3 that the index holds
  but doesn't retrieve, with the query terms their stored text files them under, and the
  evicted:kept ratio of such results on full pages;
- a sample of each hot unigram's evicted list (deep_pool.json): documents filed under a
  bigram containing it, whose own terms include it, that are not on its page;
- 1,000 random pages, to size the designs.

    uv run python scripts/combined_search_haiku/structural_probe.py
"""

import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import mmh3
import numpy as np
import zstandard
from structural_common import (
    API,
    NUM_PAGES,
    document_terms,
    fetch_terms,
    load_probe,
    load_rows,
    lookups,
    save_work,
    session,
    term_cache,
)

from mwmbl.rankeval.evaluation.haiku_arms_report import normalise_url
from mwmbl.tokenizer import tokenize

FULL_PAGE_BYTES = 3900
BIGRAMS_PER_TERM = 25
compressor = zstandard.ZstdCompressor(level=3)


def serialise(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def fetch_page(page_index: int) -> list | None:
    response = session.get(f"{API}/page/{page_index}", timeout=30)
    return response.json() if response.ok else None


def fetch_pages(page_indexes: set[int]) -> dict[int, list | None]:
    ordered = sorted(page_indexes)
    with ThreadPoolExecutor(10) as executor:
        return dict(zip(ordered, executor.map(fetch_page, ordered)))


def measure_terms(terms: set[str]) -> dict[str, dict]:
    page_of = {term: mmh3.hash(term, signed=False) % NUM_PAGES for term in terms}
    pages = fetch_pages(set(page_of.values()))
    info = {}
    for term, page_index in page_of.items():
        documents = pages[page_index]
        own = [document for document in documents if document[4] == term]
        page_bytes = sum(len(serialise(document)) for document in documents)
        info[term] = {
            "full": len(compressor.compress(serialise(documents))) >= FULL_PAGE_BYTES,
            "docs": len(documents),
            "k": len(own),
            # Raw bytes stand in for compressed ones: a page compresses fairly evenly.
            "share": sum(len(serialise(document)) for document in own) / max(1, page_bytes),
            "bigram": " " in term,
        }
    return info


def find_targets(info: dict[str, dict]) -> tuple[list[dict], dict[str, float]]:
    counts = Counter()
    targets = []
    for record in load_probe():
        if record["grade"] < 2 or not record["in_index"] or record["blacklisted"] or not record["stored"]:
            continue
        key = normalise_url(record["url"])
        filed_terms = lookups(record["query"]) & document_terms(record["stored"])
        kept = {term: any(normalise_url(d["url"]) == key for d in term_cache[term]) for term in filed_terms}
        for term, is_kept in kept.items():
            counts[(info[term]["bigram"], info[term]["full"], is_kept)] += 1
        evicted = [term for term, is_kept in kept.items() if not is_kept]
        if record["set"] == "target" and not record["candidate"] and evicted:
            words = set(tokenize(record["query"]))
            targets.append(
                {
                    "query": record["query"],
                    "url": record["url"],
                    "stored": record["stored"],
                    "evicted_terms": sorted(evicted),
                    "matches_all_words": words <= document_terms(record["stored"]) and len(words) >= 2,
                }
            )
    for bigram in (False, True):
        for full in (True, False):
            print(
                f"{'bigram' if bigram else 'unigram'} terms, {'full' if full else 'not full'} pages: "
                f"evicted {counts[(bigram, full, False)]}, kept {counts[(bigram, full, True)]}"
            )
    ratios = {
        kind: counts[(kind == "bigram", True, False)] / counts[(kind == "bigram", True, True)]
        for kind in ("unigram", "bigram")
    }
    print(f"{len(targets)} evicted targets; evicted:kept on full pages {ratios}")
    return targets, ratios


def sample_deep_pool(info: dict[str, dict]) -> dict[str, list[dict]]:
    hot_unigrams = [term for term, i in info.items() if i["full"] and not i["bigram"]]
    bigrams_for = {}
    for term in hot_unigrams:
        # Bigrams that the documents the page keeps are filed under: pages that hold more of
        # the same term's documents.
        bigrams = Counter(t for d in term_cache[term] for t in document_terms(d) if " " in t and term in t.split())
        bigrams_for[term] = [bigram for bigram, _ in bigrams.most_common(BIGRAMS_PER_TERM)]
    fetch_terms({bigram for bigrams in bigrams_for.values() for bigram in bigrams})
    pool = {}
    for term in hot_unigrams:
        on_page = {normalise_url(d["url"]) for d in term_cache[term]}
        found = {}
        for bigram in bigrams_for[term]:
            for d in term_cache[bigram]:
                key = normalise_url(d["url"])
                if key not in on_page and key not in found and term in document_terms(d):
                    found[key] = {"title": d["title"], "url": d["url"], "extract": d["extract"]}
        pool[term] = list(found.values())
    sizes = [len(documents) for documents in pool.values()]
    print(f"deep pool: {len(pool)} hot unigrams, median {np.median(sizes):.0f} documents each")
    return pool


def size_random_pages() -> None:
    pages = [page for page in fetch_pages(set(random.Random(459).sample(range(NUM_PAGES), 1000))).values() if page]
    full = [page for page in pages if len(compressor.compress(serialise(page))) >= FULL_PAGE_BYTES]
    dominant = []
    for page in full:
        term_bytes = Counter()
        for document in page:
            term_bytes[document[4]] += len(serialise(document))
        dominant.append(max(term_bytes.values()) / sum(term_bytes.values()))
    documents = [document for page in pages for document in page]
    random.Random(1).shuffle(documents)
    blocks = [documents[i : i + 64] for i in range(0, len(documents), 64)]
    bytes_per_document = sum(len(compressor.compress(serialise(block))) for block in blocks) / len(documents)
    print(
        f"random pages: {len(full)}/{len(pages)} full, {np.mean(np.array(dominant) >= 0.5):.0%} of those with one "
        f"term over half the page; {bytes_per_document:.0f} bytes per document stored once in 64-document blocks"
    )


def main():
    terms = set().union(*(lookups(row["query"]) for row in load_rows()))
    fetch_terms(terms)
    info = measure_terms(terms)
    save_work("term_info.json", info)
    targets, ratios = find_targets(info)
    save_work("targets.json", {"targets": targets, "ratios": ratios})
    save_work("deep_pool.json", sample_deep_pool(info))
    size_random_pages()


if __name__ == "__main__":
    main()
