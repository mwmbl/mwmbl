"""Step 2 of the structural-fixes study: re-rank the en-gb queries under each design.

Each design gives every hot query term a capacity, a multiple m of the documents its page
keeps today. From that:

- an evicted target comes back under a term with probability min(1, (m - 1) / R), R being
  the evicted:kept ratio of good results on full pages; an AND design always brings back,
  for its own query, a target filed under every word of a multi-word query;
- in the "deep" runs, the term's list also gets back the top (m - 1)·k of its deep-pool
  sample, in HeuristicRanker order - the order the indexer keeps a term's documents in.
  An AND design adds, for a multi-word query, every sampled document matching all its words.

Writes each design's top tens for several Monte Carlo draws to lists.json.

    uv run python scripts/combined_search_haiku/structural_simulate.py
"""

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from structural_common import document_terms, load_work, lookups, rank_all, save_work

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.tokenizer import tokenize

POSTING_BYTES = 6  # a document ID and a quantised score
DRAWS = {"targets": 12, "deep": 6}

Capacity = Callable[[dict], float]


def page_to_itself(info: dict) -> float:
    return 1 / info["share"]


def larger_pages(kilobytes: int) -> Capacity:
    """Pages this size: the other terms keep their bytes, and the hot term gets the rest."""
    return lambda info: (kilobytes / 4 - (1 - info["share"])) / info["share"]


def overflow_extent(kilobytes: int) -> Capacity:
    return lambda info: 1 + kilobytes / (4 * info["share"])


def id_postings(fetch_budget: int) -> Capacity:
    """Document IDs on the 4 KB page, with at most fetch_budget documents read per term."""

    def capacity(info: dict) -> float:
        postings = (4096 - (info["docs"] - info["k"]) * POSTING_BYTES) / POSTING_BYTES
        return min(postings, fetch_budget) / info["k"]

    return capacity


def constant(multiple: float) -> Capacity:
    return lambda info: multiple


@dataclass
class Design:
    one_word: Capacity
    multi_word: Capacity
    conjunctive: bool = False


DESIGNS = {
    "#459 per-page fixes": Design(constant(1.195), constant(1.195)),
    "Hot term gets its page to itself": Design(page_to_itself, page_to_itself),
    "8 KB pages": Design(larger_pages(8), larger_pages(8)),
    "Document IDs on 4 KB pages, fetch ≤100 per term": Design(id_postings(100), id_postings(100)),
    "16 KB overflow extent for hot terms": Design(overflow_extent(16), overflow_extent(16)),
    "Document IDs on 4 KB pages, fetch ≤300 per term": Design(id_postings(300), id_postings(300)),
    "Inverted index, AND + ≤300 per term": Design(id_postings(300), id_postings(300), conjunctive=True),
    "Inverted index, AND only for multi-word, ≤100 per term for one word": Design(
        id_postings(100), constant(1.0), conjunctive=True
    ),
    "64 KB overflow extent for hot terms": Design(overflow_extent(64), overflow_extent(64)),
    "No evictions": Design(constant(math.inf), constant(math.inf)),
}


def ordered_pools(info: dict[str, dict], pool: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Each hot term's deep sample, best first by the indexer's own ordering."""
    ordered = {}
    for term, term_info in info.items():
        if not term_info["full"] or term_info["k"] == 0:
            continue
        if term_info["bigram"]:
            first, second = term.split(" ", 1)
            candidates = {d["url"]: d for d in pool.get(first, []) + pool.get(second, []) if term in document_terms(d)}
            documents = list(candidates.values())
        else:
            documents = pool.get(term, [])
        words = term.split()
        ordered[term] = sorted(
            documents, key=lambda d: -score_result(words, Document(d["title"], d["url"], d["extract"]), True)
        )
    return ordered


def expected_recovery(design: Design, info, ratios, targets) -> float:
    """The share of evicted targets a design brings back, in expectation."""
    shares = []
    for target in targets:
        if design.conjunctive and target["matches_all_words"]:
            shares.append(1.0)
            continue
        multiple = design.multi_word if len(tokenize(target["query"])) >= 2 else design.one_word
        missed = 1.0
        for term in target["evicted_terms"]:
            ratio = ratios["bigram" if info[term]["bigram"] else "unigram"]
            missed *= 1 - min(1.0, (multiple(info[term]) - 1) / ratio)
        shares.append(1 - missed)
    return float(np.mean(shares))


def simulate(design: Design, mode: str, rng, info, ratios, targets, ordered) -> dict[str, list[str]]:
    def capacity(term: str, query: str) -> float:
        multiple = design.multi_word if len(tokenize(query)) >= 2 else design.one_word
        return multiple(info[term])

    def recovery(term: str, query: str) -> float:
        ratio = ratios["bigram" if info[term]["bigram"] else "unigram"]
        return min(1.0, (capacity(term, query) - 1) / ratio)

    # A target recovered through a term's list is in that list for every query that reads it;
    # one recovered by AND retrieval is only found by its own query.
    shared: dict[str, list[dict]] = {}
    own: dict[str, list[dict]] = {}
    for target in targets:
        if design.conjunctive and target["matches_all_words"]:
            own.setdefault(target["query"], []).append(target)
            continue
        for term in target["evicted_terms"]:
            if rng.random() < recovery(term, target["query"]):
                shared.setdefault(term, []).append(target["stored"])

    def injection(query: str) -> dict[str, list[dict]]:
        injected = {term: list(documents) for term, documents in shared.items()}
        words = tokenize(query)
        for target in own.get(query, []):
            injected.setdefault(target["evicted_terms"][0], []).append(target["stored"])
        if mode == "deep":
            for term in lookups(query) & ordered.keys():
                multiple = capacity(term, query)
                count = len(ordered[term]) if math.isinf(multiple) else round((multiple - 1) * info[term]["k"])
                injected.setdefault(term, []).extend(ordered[term][:count])
            if design.conjunctive and len(words) >= 2:
                matching = [d for word in words for d in ordered.get(word, []) if set(words) <= document_terms(d)]
                injected.setdefault(words[0], []).extend(matching)
        return injected

    return rank_all(injection)


def main():
    info = load_work("term_info.json")
    probe = load_work("targets.json")
    targets, ratios = probe["targets"], probe["ratios"]
    ordered = ordered_pools(info, load_work("deep_pool.json"))
    rng = np.random.default_rng(0)
    lists = {"baseline": rank_all(lambda query: {}), "targets": {}, "deep": {}}
    lists["recovered"] = {name: expected_recovery(design, info, ratios, targets) for name, design in DESIGNS.items()}
    for mode, draws in DRAWS.items():
        for name, design in DESIGNS.items():
            lists[mode][name] = [simulate(design, mode, rng, info, ratios, targets, ordered) for _ in range(draws)]
            print(f"{mode}: {name}", flush=True)
    save_work("lists.json", lists)


if __name__ == "__main__":
    main()
