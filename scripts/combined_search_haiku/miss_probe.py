"""Why does the index miss Brave's good results? Assign each miss to the first stage that fails.

For every Brave top-ten URL of the en-gb rows that Haiku graded >= 2 for a UK searcher and
that the index-only ranking does not put in its own top ten (the targets), and for the Brave
URLs it does put there (the controls, which check the probe), this records:

1. Whether the URL is in the index at all. A document is filed under the first ten tokens
   and bigrams of its title, URL and extract (tokenize_document), so looking up those terms
   for Brave's title and snippet and the URL reads every page it would be stored on.
2. Whether it is in the query's candidate set (Ranker.retrieve).
3. If it is, what the ranking did with it: the LTR score, whether the majority-terms filter
   zeroed it, and its rank in the index-only list.

The index-only ranking is Combined Search with nothing from Staan, which is what production
serves when Staan returns nothing. Everything goes through RemoteIndex against
api.mwmbl.org, cached on disk, so a rerun is free and reproduces the same answers.

    DATABASE_URL="postgres://daoud@" uv run python scripts/combined_search_haiku/miss_probe.py
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import django

django.setup()

from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.indexer.index import tokenize_document
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.haiku_arms_report import normalise_url
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.search_setup import combined_ltr_model
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.rank import get_features
from mwmbl.utils import get_domain

EVAL_DIR = Path("devdata/combined_providers_eval")
OUTPUT_PATH = EVAL_DIR / "engb/miss_probe.json"
NUM_RESULTS = 10
GOOD_GRADE = 2
THREADS = 16


def host(url: str) -> str:
    return normalise_url(url).split("/")[0]


def document_record(doc) -> dict:
    return {"url": doc.url, "title": doc.title, "extract": doc.extract, "term": doc.term}


def main():
    rows = json.loads((EVAL_DIR / "engb/rows-0.05.json").read_text())
    pool_text = json.loads((EVAL_DIR / "engb/pool_text.json").read_text())
    grades = {}
    for line in (EVAL_DIR / "haiku/relevance_engb.jsonl").read_text().splitlines():
        record = json.loads(line)
        grades[(record["query"], record["url"])] = record["relevance"]

    remote_index = RemoteIndex()
    inner = CombinedLTRRanker(remote_index, DummyCompleter(), combined_ltr_model)
    ranker = MMRRanker(inner)
    blacklist = get_snapshot_blacklist()

    # Every Brave URL we need to probe, with the terms it would be filed under.
    items = []
    for row in rows:
        query = row["query"]
        for position, url in enumerate(row["lists"]["brave"][:NUM_RESULTS], start=1):
            grade = grades.get((query, url))
            if grade is None:
                continue
            title, extract, _ = pool_text[query][url]
            tokens = tokenize_document(url, title or "", extract or "", 0.0).tokens
            items.append({"query": query, "url": url, "position": position, "grade": grade, "probe_terms": tokens})

    # One lookup per distinct term, shared between items and queries.
    probe_terms = sorted({term for item in items for term in item["probe_terms"]})
    print(f"{len(items)} Brave URLs, {len(probe_terms)} distinct probe terms", flush=True)
    with ThreadPoolExecutor(THREADS) as executor:
        term_docs = dict(zip(probe_terms, executor.map(remote_index.retrieve, probe_terms)))

    print("probe terms fetched", flush=True)

    # The index-only ranking and its candidate set, per query.
    per_query = {}
    for i, row in enumerate(rows):
        if i % 50 == 0:
            print(f"ranking query {i}", flush=True)
        query = row["query"]
        retrieval = inner.retrieve(query)
        final = ranker.search(query, [], False)
        candidates = retrieval.pages
        predictions = inner.model.predict(inner.records(" ".join(retrieval.terms), candidates)) if candidates else []
        per_query[query] = {
            "terms": retrieval.terms,
            "final": [normalise_url(doc.url) for doc in final],
            "candidates": candidates,
            "predictions": list(map(float, predictions)),
        }

    domains = {get_domain(item["url"]) for item in items}
    blacklisted_domains = blacklist.filter_blacklisted(domains)

    records = []
    for item in items:
        query, url = item["query"], item["url"]
        key, url_host = normalise_url(url), host(url)
        info = per_query[query]
        index_rank = info["final"].index(key) + 1 if key in info["final"] else None
        in_top = index_rank is not None and index_rank <= NUM_RESULTS

        stored, stored_terms, host_seen = None, [], False
        for term in item["probe_terms"]:
            for doc in term_docs[term]:
                if normalise_url(doc.url) == key:
                    stored = stored or document_record(doc)
                    stored_terms.append(term)
                elif host(doc.url) == url_host:
                    host_seen = True

        candidate_scores = [
            (doc, score) for doc, score in zip(info["candidates"], info["predictions"]) if normalise_url(doc.url) == key
        ]
        record = {
            **{k: item[k] for k in ("query", "url", "position", "grade")},
            "set": "control" if in_top else ("target" if item["grade"] >= GOOD_GRADE else None),
            "index_rank": index_rank,
            "in_index": stored is not None or bool(candidate_scores) or index_rank is not None,
            "stored": stored,
            "stored_terms": stored_terms,
            "host_in_index": host_seen,
            "blacklisted": get_domain(url) in blacklisted_domains,
            "candidate": bool(candidate_scores),
            "query_terms": info["terms"],
        }
        if candidate_scores:
            doc, score = max(candidate_scores, key=lambda pair: pair[1])
            features = get_features(info["terms"], doc.title or "", doc.url, doc.extract or "", doc.score or 0.0, True)
            record["stored"] = record["stored"] or document_record(doc)
            record["ltr_score"] = score
            record["match_terms"] = features["match_terms"]
            record["majority_filtered"] = features["match_terms"] <= len(info["terms"]) / 2
        # Which of its tokens overlap the query's lookups: overlap without retrieval means
        # it was pushed off that term's page; none means it is filed under other words.
        query_lookups = set(info["terms"]) | {" ".join(pair) for pair in zip(info["terms"], info["terms"][1:])}
        record["probe_terms_overlapping_query"] = sorted(query_lookups & set(item["probe_terms"]))
        records.append(record)

    OUTPUT_PATH.write_text(json.dumps(records, indent=1))
    counts = {name: sum(r["set"] == name for r in records) for name in ("target", "control")}
    print(f"{counts} -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
