"""Collect the documents that compete for the en-gb eval queries' index pages.

The A/B indexes one fixed corpus twice, once per write-time ranker. Only the pages the eval
queries read matter: every query token, bigram and the whole query (Ranker.retrieve). So
the corpus is every document that tokenize_document would file under one of those terms,
from two places:

- the local crawl batches (devdata/batches, 2023-11 to 2024-07, ~32M documents): real
  competition, including the documents the live index has already evicted;
- the live index's current documents on every page miss_probe.py read (RemoteIndex cache,
  no network): today's text, and the evicted targets, which survive under other terms.

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" PYTHONPATH=. \
        uv run python scripts/index_write_order/build_corpus.py [--limit N]
"""

import gzip
import json
import os
from argparse import ArgumentParser
from multiprocessing import Pool
from pathlib import Path

import django

django.setup()

from mwmbl.indexer.index import tokenize_document
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tokenizer import get_bigrams, tokenize

EVAL_DIR = Path("devdata/combined_providers_eval")
# The first A/B read the 2023-24 batches. The "fresh" run reads run_crawl.py's crawl from the
# day the crawl model's negatives stop (crawl_model.TRAIN_BEFORE), so the two never share one.
RUN = os.environ.get("INDEX_WRITE_ORDER_RUN", "")
# INDEX_WRITE_ORDER_QUERIES=train collects for the query-time model's LLM-labelled training
# queries instead (retrain.py), into a train/ subdirectory.
QUERIES = os.environ.get("INDEX_WRITE_ORDER_QUERIES", "eval")
OUT_DIR = Path("devdata/index_write_order") / RUN / ("train" if QUERIES == "train" else "")
FRESH_SINCE = "2026-09-25"
PROCESSES = int(os.environ.get("INDEX_WRITE_ORDER_PROCESSES", 8))


def lookup_terms(query: str) -> set[str]:
    terms = tokenize(query)
    return set(terms) | set(get_bigrams(len(terms), terms)) | {" ".join(terms)}


def eval_queries() -> set[str]:
    rows = json.loads((EVAL_DIR / "engb/rows-0.05.json").read_text())
    return {row["query"] for row in rows}


def train_queries() -> set[str]:
    """The LLM-labelled queries the combined model trains on. None is an eval query."""
    import pandas as pd

    from mwmbl.rankeval.paths import LEARNING_TO_RANK_LLM_DATASET_PATH

    llm = pd.read_csv(LEARNING_TO_RANK_LLM_DATASET_PATH, lineterminator="\n")
    return set(llm["query"].str.lower().str.strip()) - {query.lower().strip() for query in eval_queries()}


QUERY_SET = train_queries() if QUERIES == "train" else eval_queries()
LOOKUP = set().union(*(lookup_terms(query) for query in QUERY_SET))


def filed_terms(url: str, title: str, extract: str) -> list[str]:
    return sorted(set(tokenize_document(url, title, extract, 0.0).tokens) & LOOKUP)


def read_batch(path: Path) -> list[dict] | None:
    try:
        with gzip.open(path) as file:
            batch = json.load(file)
    except (json.JSONDecodeError, EOFError, gzip.BadGzipFile):
        # A download that was cut short: a few files in the local cache are empty.
        return None
    kept = []
    for item in batch["items"]:
        content = item.get("content")
        if content is None or content.get("links_only") or not content.get("title"):
            continue
        extract = content.get("extract") or ""
        terms = filed_terms(item["url"], content["title"], extract)
        if terms:
            kept.append(
                {
                    "url": item["url"],
                    "title": content["title"],
                    "extract": extract,
                    "last_crawled": int(item["timestamp"] / 1000),
                    "terms": terms,
                    "source": "batch",
                }
            )
    return kept


def compressed_batch(path: Path) -> tuple[int, bytes] | None:
    """read_batch's documents as one gzip member, so the workers do the compressing: a single
    writer falls behind them, and the queued results exhaust memory on the training corpus.
    Concatenated gzip members are a valid gzip file."""
    documents = read_batch(path)
    if documents is None:
        return None
    return len(documents), gzip.compress("".join(json.dumps(document) + "\n" for document in documents).encode())


def live_documents() -> list[dict]:
    # The training queries were never probed: their terms' pages are all there is.
    probe = json.loads((EVAL_DIR / "engb/miss_probe.json").read_text()) if QUERIES == "eval" else []
    probed_terms = sorted({term for record in probe for term in record["query_terms"]} | LOOKUP)
    remote_index = RemoteIndex()
    by_url = {}
    for term in probed_terms:
        for doc in remote_index.retrieve(term):
            by_url.setdefault(doc.url, doc)
    # The stored text of the Brave URLs the probe found in the index, under any term.
    for record in probe:
        stored = record["stored"]
        if stored is not None and stored["url"] not in by_url:
            by_url[stored["url"]] = stored
    kept = []
    for url, doc in by_url.items():
        title, extract = (doc["title"], doc["extract"]) if isinstance(doc, dict) else (doc.title, doc.extract)
        terms = filed_terms(url, title or "", extract or "")
        if title and terms:
            kept.append({"url": url, "title": title, "extract": extract or "", "terms": terms, "source": "live"})
    return kept


def main():
    parser = ArgumentParser()
    parser.add_argument("--limit", type=int, help="read only this many batch files, to time a run")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if RUN == "fresh":
        crawl_dir = Path("devdata/index_write_order/crawl")
        days = [day for day in sorted(crawl_dir.iterdir()) if day.name >= FRESH_SINCE]
        paths = sorted(path for day in days for path in day.glob("*.json.gz"))[: args.limit]
    else:
        paths = sorted(Path("devdata/batches").rglob("*.json.gz"))[: args.limit]
    print(f"{len(LOOKUP)} lookup terms, {len(paths)} batch files", flush=True)

    live = live_documents()
    print(f"{len(live)} live documents", flush=True)
    num_batch_documents = 0
    num_unreadable = 0
    with open(OUT_DIR / "corpus.jsonl.gz", "wb") as out, Pool(PROCESSES) as pool:
        out.write(gzip.compress("".join(json.dumps(document) + "\n" for document in live).encode()))
        for i, result in enumerate(pool.imap_unordered(compressed_batch, paths, chunksize=64)):
            if i % 10000 == 0:
                print(f"batch file {i}, {num_batch_documents} documents kept", flush=True)
            if result is None:
                num_unreadable += 1
                continue
            num_documents, member = result
            out.write(member)
            num_batch_documents += num_documents
    print(f"{num_unreadable} unreadable batch files")
    print(f"done: {len(live)} live + {num_batch_documents} batch documents -> {OUT_DIR / 'corpus.jsonl.gz'}")


if __name__ == "__main__":
    main()
