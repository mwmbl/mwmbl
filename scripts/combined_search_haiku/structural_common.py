"""Shared pieces of the structural-fixes study (docs/index-structural-fixes.md).

The index-only ranking of #454 and #459 - CombinedLTRRanker + MMR over the live index's
/search/raw results - with extra documents injected into given terms' lists, and NDCG@10
against the en-gb UK-relevance Haiku grades. Term lookups are cached in WORK_DIR, so a
rerun sees the same index snapshot.

Run from the repository root with this directory on the path; needs no database, Redis or
API keys:

    PYTHONPATH=scripts/combined_search_haiku:. uv run python scripts/combined_search_haiku/structural_probe.py
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings_structural")
# Blacklisted targets are excluded up front; the filter would only add a Redis read per query.
os.environ["BLACKLIST_FILTER_AT_RETRIEVAL"] = "false"

import django

django.setup()

import requests
from django.conf import settings

from mwmbl.indexer.index import tokenize_document
from mwmbl.rankeval.evaluation.haiku_arms_report import ndcg, normalise_url
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tokenizer import get_bigrams, tokenize

EVAL_DIR = Path("devdata/combined_providers_eval")
WORK_DIR = EVAL_DIR / "structural_work"
GRADES_PATH = EVAL_DIR / "structural/grades_unjudged.jsonl"
API = "https://api.mwmbl.org/api/v1/search"
NUM_PAGES = 102_400_000  # settings_prod.NUM_PAGES
NUM_RESULTS = 10
THREADS = 8

session = requests.Session()
model = RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))


def work_path(name: str) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return WORK_DIR / name


def load_work(name: str):
    return json.loads(work_path(name).read_text())


def save_work(name: str, value) -> None:
    work_path(name).write_text(json.dumps(value))


TERM_CACHE_PATH = work_path("term_cache.json")
term_cache: dict[str, list[dict]] = json.loads(TERM_CACHE_PATH.read_text()) if TERM_CACHE_PATH.exists() else {}


def fetch_term(term: str) -> list[dict]:
    response = session.get(f"{API}/raw", params={"s": term}, timeout=30)
    response.raise_for_status()
    return response.json()["results"]


def fetch_terms(terms: set[str]) -> None:
    """Read every term not already cached, and save the cache."""
    missing = sorted(terms - term_cache.keys())
    with ThreadPoolExecutor(16) as executor:
        term_cache.update(zip(missing, executor.map(fetch_term, missing)))
    TERM_CACHE_PATH.write_text(json.dumps(term_cache))


def lookups(query: str) -> set[str]:
    """The terms Ranker.retrieve reads for a complete query: its words and their bigrams."""
    words = tokenize(query)
    return set(words) | set(get_bigrams(len(words), words))


_document_terms: dict[str, set[str]] = {}


def document_terms(document: dict) -> set[str]:
    """The terms the crawl indexer files a document under, from the text it stored."""
    url = document["url"]
    if url not in _document_terms:
        tokenized = tokenize_document(url, document["title"] or "", document["extract"] or "", 0.0)
        _document_terms[url] = set(tokenized.tokens)
    return _document_terms[url]


class InjectingIndex:
    """The cached live index, with extra documents appended to some terms' lists."""

    def __init__(self, injected: dict[str, list[dict]]):
        self.injected = injected

    def retrieve(self, term: str) -> list[Document]:
        if term not in term_cache:
            term_cache[term] = fetch_term(term)
        documents = [Document(**result) for result in term_cache[term]]
        present = {normalise_url(document.url) for document in documents}
        for record in self.injected.get(term, []):
            if normalise_url(record["url"]) not in present:
                documents.append(Document(record["title"], record["url"], record["extract"], term=term))
        return documents


class DummyCompleter:
    def complete(self, prefix):
        return [prefix]


def rank(query: str, injected: dict[str, list[dict]]) -> list[str]:
    """The index-only top ten for the query, with the injected documents in the index."""
    ranker = MMRRanker(CombinedLTRRanker(InjectingIndex(injected), DummyCompleter(), model))
    return [document.url for document in ranker.search(query, [], False)][:NUM_RESULTS]


def rank_all(injection_for_query) -> dict[str, list[str]]:
    """Every query's index-only top ten, with injection_for_query(query) in the index."""

    def one(row):
        return row["query"], rank(row["query"], injection_for_query(row["query"]))

    with ThreadPoolExecutor(THREADS) as executor:
        return dict(executor.map(one, load_rows()))


def load_rows() -> list[dict]:
    return json.loads((EVAL_DIR / "engb/rows-0.05.json").read_text())


def load_grades() -> dict[str, dict[str, int]]:
    grades: dict[str, dict[str, int]] = {}
    for line in (EVAL_DIR / "haiku/relevance_engb.jsonl").read_text().splitlines():
        record = json.loads(line)
        grades.setdefault(record["query"], {})[normalise_url(record["url"])] = record["relevance"]
    return grades


def load_probe() -> list[dict]:
    return json.loads((EVAL_DIR / "engb/miss_probe.json").read_text())


def query_ndcg(urls: list[str], graded: dict[str, int], unjudged_gain: float) -> float:
    """NDCG@10 against every judged URL for the query, URLs compared after normalise_url."""
    keys = list(dict.fromkeys(normalise_url(url) for url in urls))[:NUM_RESULTS]
    gains = [2 ** graded[key] - 1 if key in graded else unjudged_gain for key in keys]
    return ndcg(gains, [2**grade - 1 for grade in graded.values()])


def staan_first(staan: list[str], index_only: list[str]) -> list[str]:
    """Production: Staan's top ten, then the index's results Staan didn't return."""
    staan_keys = {normalise_url(url) for url in staan}
    return staan + [url for url in index_only if normalise_url(url) not in staan_keys]
