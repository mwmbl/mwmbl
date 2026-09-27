"""The crawl model: an LTR model for ordering a term's index page, trained against what was crawled.

The query-time models learn to order a pool that the heuristic has already filtered, so at
write time, asked about a single term, they have never seen the spam the heuristic keeps out
(index-write-order.md). This model keeps the combined model's positives, re-expressed as
(term, document) for each lookup term of the query that the document is filed under, and adds
to its negatives a sample of every crawled page filed under the same term.

Beyond the Rust features it can use domain information the query-time models do not:

- `curated`: the host, or a parent of it, is a moderator-approved domain;
- `serp_queries_host`, `serp_top3_host`, `serp_queries_apex`: how many of the Firefox
  extension's Google SERPs, from every period, the host appears in (in the top three; or its
  registered domain does), leaving out the row's own query and every eval query;
- the shape of the host: labels, digits, hyphens, a numbered first label.

Subcommands, run from the repository root with
`DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" PYTHONPATH=scripts/index_write_order:.`:

- `serp`: count each host's appearances in the downloaded extension scrapes, into
  serp_domains.json.
- `negatives`: sample crawled documents per training term from the crawl days before
  TRAIN_BEFORE (run_crawl.py's output), into negatives.jsonl.gz.
- `train [--no-domain]`: build the (term, document) rows, report the offline gate on held-out
  terms against the heuristic and the combined model, and save the model.
"""

import gzip
import json
import random
import re
import zlib
from argparse import ArgumentParser
from collections import defaultdict
from functools import cache
from multiprocessing import Pool
from pathlib import Path
from urllib.parse import urlparse

import django

django.setup()

import numpy as np
import pandas as pd
import requests
import xgboost as xgb
from django.conf import settings
from sklearn.metrics import roc_auc_score

import mwmbl_rank
from mwmbl.indexer.index import tokenize_document
from mwmbl.rankeval.dataset.extension_dataset import DOWNLOADS_DIR
from mwmbl.rankeval.ltr.provider_features import training_frame
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.tokenizer import get_bigrams, tokenize

OUT_DIR = Path("devdata/index_write_order")
CRAWL_DIR = OUT_DIR / "crawl"
NEGATIVES_PATH = OUT_DIR / "negatives.jsonl.gz"
CURATED_PATH = OUT_DIR / "curated_domains.json"
SERP_DOMAINS_PATH = OUT_DIR / "serp_domains.json"
EVAL_DIR = Path("devdata/combined_providers_eval")
MODEL_DIR = OUT_DIR / "models"
# Crawl days before this are the model's negatives; this day on is the rerun's corpus, so the
# two never share a crawl.
TRAIN_BEFORE = "2026-09-25"
NEGATIVES_PER_TERM = 200
TEST_TERM_FRACTION = 0.2
PROCESSES = 8
SEED = 0

# Registered domains one level below these public suffixes, e.g. bbc.co.uk.
SECOND_LEVEL_SUFFIXES = {"co", "ac", "gov", "org", "com", "net", "edu", "ne", "or", "go", "nhs", "ltd", "plc"}
NUMBERED_LABEL = re.compile(r"[a-z]\d{3,}|\d{3,}[a-z]")

DOMAIN_FEATURE_NAMES = [
    "curated",
    "serp_queries_host",
    "serp_top3_host",
    "serp_queries_apex",
    "host_labels",
    "host_digits",
    "host_hyphens",
    "first_label_length",
    "first_label_numbered",
    "is_www",
]


def lookup_terms(query: str) -> set[str]:
    terms = tokenize(query)
    return set(terms) | set(get_bigrams(len(terms), terms)) | {" ".join(terms)}


def filed_terms(url: str, title: str, extract: str) -> set[str]:
    return set(tokenize_document(url, title, extract, 0.0).tokens)


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def apex_of(host: str) -> str:
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in SECOND_LEVEL_SUFFIXES and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


# --- Negatives ------------------------------------------------------------------------------

_TERMS: set[str] = set()


def _init_worker(terms: set[str]) -> None:
    global _TERMS
    _TERMS = terms


def _read_batch(path: Path) -> list[dict]:
    with gzip.open(path) as file:
        batch = json.load(file)
    kept = []
    for item in batch["items"]:
        content = item.get("content")
        if content is None or content.get("links_only") or not content.get("title"):
            continue
        extract = content.get("extract") or ""
        terms = filed_terms(item["url"], content["title"], extract) & _TERMS
        if terms:
            kept.append({"url": item["url"], "title": content["title"], "extract": extract, "terms": sorted(terms)})
    return kept


def crawl_paths(before: str | None = None, since: str | None = None) -> list[Path]:
    days = sorted(path for path in CRAWL_DIR.iterdir() if path.is_dir())
    days = [day for day in days if (before is None or day.name < before) and (since is None or day.name >= since)]
    return sorted(path for day in days for path in day.glob("*.json.gz"))


def build_negatives() -> None:
    frame = training_frame()
    terms = set().union(*(lookup_terms(query) for query in frame["query"].unique()))
    paths = crawl_paths(before=TRAIN_BEFORE)
    print(f"{len(terms)} training terms, {len(paths)} crawl batch files", flush=True)

    # One reservoir per term, so a common term does not crowd out the rest.
    rng = random.Random(SEED)
    reservoirs: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, int] = defaultdict(int)
    seen_urls = set()
    with Pool(PROCESSES, initializer=_init_worker, initargs=(terms,)) as pool:
        for documents in pool.imap_unordered(_read_batch, paths, chunksize=16):
            for document in documents:
                if document["url"] in seen_urls:
                    continue
                seen_urls.add(document["url"])
                record = {key: document[key] for key in ("url", "title", "extract")}
                for term in document["terms"]:
                    seen[term] += 1
                    reservoir = reservoirs[term]
                    if len(reservoir) < NEGATIVES_PER_TERM:
                        reservoir.append(record)
                    else:
                        slot = rng.randrange(seen[term])
                        if slot < NEGATIVES_PER_TERM:
                            reservoir[slot] = record

    with gzip.open(NEGATIVES_PATH, "wt") as out:
        for term, reservoir in reservoirs.items():
            for record in reservoir:
                out.write(json.dumps({"term": term, **record, "competitors": seen[term]}) + "\n")
    covered = sum(1 for term in terms if term in reservoirs)
    print(f"{len(seen_urls)} unique crawled documents; {covered} of {len(terms)} terms have negatives")


# --- Domain features ------------------------------------------------------------------------


@cache
def curated_domains() -> frozenset[str]:
    if not CURATED_PATH.exists():
        response = requests.get("https://api.mwmbl.org/api/v1/crawler/curated-domains", timeout=30)
        response.raise_for_status()
        CURATED_PATH.write_text(json.dumps(sorted(domain["name"] for domain in response.json()["domains"])))
    return frozenset(json.loads(CURATED_PATH.read_text()))


def normalise_query(query: str) -> str:
    return " ".join(tokenize(query))


def build_serp_domains() -> None:
    """host -> {query: best rank} over every extension scrape downloaded to scripts/downloads,
    from all periods. The eval queries are left out entirely, so the counts carry nothing about
    the queries the rerun is judged on."""
    rows = json.loads((EVAL_DIR / "engb/rows-0.05.json").read_text())
    eval_queries = {normalise_query(row["query"]) for row in rows}
    by_host: dict[str, dict[str, int]] = defaultdict(dict)
    num_files = 0
    num_skipped = 0
    for path in DOWNLOADS_DIR.glob("**/dataset/**/*.json.gz"):
        with gzip.open(path) as file:
            data = json.load(file)
        num_files += 1
        for item in data.get("searchResults", []):
            query = normalise_query(item["query"])
            if not query or query in eval_queries:
                num_skipped += query in eval_queries
                continue
            for rank, result in enumerate(item["results"], 1):
                ranks = by_host[host_of(result["url"])]
                ranks[query] = min(rank, ranks.get(query, rank))
    SERP_DOMAINS_PATH.write_text(json.dumps(by_host))
    num_queries = len({query for ranks in by_host.values() for query in ranks})
    print(f"{num_files} files, {num_queries} queries, {len(by_host)} hosts; {num_skipped} eval-query SERPs left out")


@cache
def serp_domains() -> tuple[dict[str, dict[str, int]], dict[str, set[str]]]:
    by_host = json.loads(SERP_DOMAINS_PATH.read_text())
    by_apex = defaultdict(set)
    for host, ranks in by_host.items():
        by_apex[apex_of(host)].update(ranks)
    return by_host, by_apex


def domain_features(url: str, query: str | None) -> list[float]:
    """`query` is the row's own query, left out of the SERP counts so that a SERP-presence
    positive cannot see its own label; None for a crawled negative."""
    host = host_of(url)
    apex = apex_of(host)
    by_host, by_apex = serp_domains()
    host_ranks = by_host.get(host, {})
    own = normalise_query(query) if query is not None else None
    host_queries = [other for other in host_ranks if other != own]
    apex_queries = by_apex.get(apex, set()) - {own}
    curated = curated_domains()
    labels = host.split(".")
    first_label = labels[1] if labels[0] == "www" and len(labels) > 2 else labels[0]
    return [
        float(host in curated or apex in curated or host.removeprefix("www.") in curated),
        float(len(host_queries)),
        float(sum(1 for other in host_queries if host_ranks[other] <= 3)),
        float(len(apex_queries)),
        float(len(labels)),
        float(sum(char.isdigit() for char in host)),
        float(host.count("-")),
        float(len(first_label)),
        float(bool(NUMBERED_LABEL.search(first_label)) and host != apex),
        float(labels[0] == "www"),
    ]


# --- Rows -----------------------------------------------------------------------------------


def term_rows() -> pd.DataFrame:
    """(term, document) rows: the combined model's rows re-filed under each lookup term of
    their query that the document is filed under, plus the sampled crawl negatives."""
    frame = training_frame()
    rows = []
    for query, url, title, extract, label, weight in zip(
        frame["query"], frame["url"], frame["title"], frame["extract"], frame["label"], frame["weight"]
    ):
        qnorm = query.lower().strip()
        for term in lookup_terms(query) & filed_terms(url, title, extract):
            rows.append((term, url, title, extract, label, weight, qnorm, "frame"))
    with gzip.open(NEGATIVES_PATH, "rt") as file:
        for line in file:
            record = json.loads(line)
            rows.append((record["term"], record["url"], record["title"], record["extract"], 0.0, 1.0, None, "crawl"))
    columns = ["term", "url", "title", "extract", "label", "weight", "qnorm", "origin"]
    return pd.DataFrame(rows, columns=columns)


FEATURE_CHUNK = 20_000


def all_features(rows: pd.DataFrame, use_domain: bool) -> np.ndarray:
    """Built a chunk at a time into one float32 array: extract_features returns Python lists,
    which for every row at once take several times the memory of the matrix itself."""
    num_columns = len(feature_names(use_domain))
    features = np.empty((len(rows), num_columns), dtype=np.float32)
    for start in range(0, len(rows), FEATURE_CHUNK):
        chunk = rows.iloc[start : start + FEATURE_CHUNK]
        # score is the retrieval score a pool row came with, which a document being written
        # has not got: zero for every row, as index_pages sees it.
        records = [
            {"query": term, "url": url, "title": title, "extract": extract, "score": 0.0}
            for term, url, title, extract in zip(chunk["term"], chunk["url"], chunk["title"], chunk["extract"])
        ]
        end = start + len(chunk)
        features[start:end, : mwmbl_rank.NUM_FEATURES] = mwmbl_rank.RustXGBPipeline.extract_features(records, False)
        if use_domain:
            features[start:end, mwmbl_rank.NUM_FEATURES :] = [
                domain_features(url, qnorm) for url, qnorm in zip(chunk["url"], chunk["qnorm"])
            ]
    return features


def feature_names(use_domain: bool) -> list[str]:
    return list(mwmbl_rank.FEATURE_NAMES) + (DOMAIN_FEATURE_NAMES if use_domain else [])


def is_test_term(term: str) -> bool:
    return zlib.crc32(term.encode()) % 1000 < TEST_TERM_FRACTION * 1000


# --- Offline gate ---------------------------------------------------------------------------


def heuristic_scores(rows: pd.DataFrame) -> np.ndarray:
    return np.array(
        [
            score_result(term.split(), Document(title, url, extract), True)
            for term, url, title, extract in zip(rows["term"], rows["url"], rows["title"], rows["extract"])
        ]
    )


def combined_scores(rows: pd.DataFrame) -> np.ndarray:
    model = RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))
    records = [
        {"query": term, "url": url, "title": title, "extract": extract, "score": 0.0}
        for term, url, title, extract in zip(rows["term"], rows["url"], rows["title"], rows["extract"])
    ]
    return model.predict(records)


def gate(test: pd.DataFrame, scores: dict[str, np.ndarray]) -> pd.DataFrame:
    """Per held-out term, how well each scorer puts the positives above the negatives,
    averaged over terms. Against crawled pages is the question the page order answers; against
    the pools' graded negatives is the harder one the query-time models were trained on."""
    results = defaultdict(lambda: defaultdict(list))
    for _, group in test.groupby("term"):
        positive = group["label"] > 0.5
        if not positive.any():
            continue
        for negatives_name, negative in [
            ("vs crawl", group["origin"] == "crawl"),
            ("vs pool negatives", (group["origin"] == "frame") & ~positive),
        ]:
            if not negative.any():
                continue
            mask = (positive | negative).to_numpy()
            labels = positive.to_numpy()[mask]
            for name, score in scores.items():
                results[name][negatives_name].append(roc_auc_score(labels, score[group.index][mask]))
    table = pd.DataFrame(
        {
            name: {negatives: np.mean(aucs) for negatives, aucs in by_negatives.items()}
            for name, by_negatives in results.items()
        }
    ).T
    table["terms"] = len(next(iter(results.values()))["vs crawl"])
    return table


def train(use_domain: bool) -> None:
    rows = term_rows()
    test_mask = rows["term"].map(is_test_term).to_numpy()
    print(
        f"{len(rows)} rows: {int((rows['label'] > 0.5).sum())} positive, "
        f"{int((rows['origin'] == 'crawl').sum())} crawled negatives; {test_mask.sum()} held out",
        flush=True,
    )
    features = all_features(rows, use_domain)
    names = feature_names(use_domain)
    train_rows = rows[~test_mask]
    train_matrix = xgb.DMatrix(
        features[~test_mask], label=train_rows["label"], weight=train_rows["weight"], feature_names=names
    )
    params = {"objective": "binary:logistic", "eval_metric": "logloss", "lambda": 2.0, "seed": SEED}
    booster = xgb.train(params, train_matrix, num_boost_round=200)

    test = rows[test_mask].reset_index(drop=True)
    test_features = features[test_mask]
    scores = {
        "heuristic": heuristic_scores(test),
        "combined (ltr arm)": combined_scores(test),
        "crawl": booster.predict(xgb.DMatrix(test_features, feature_names=names)),
    }
    print(gate(test, scores).round(3).to_string())
    importance = booster.get_score(importance_type="gain")
    print("top features by gain:", sorted(importance.items(), key=lambda item: -item[1])[:15])

    MODEL_DIR.mkdir(exist_ok=True)
    path = MODEL_DIR / ("crawl-domain.json" if use_domain else "crawl.json")
    booster.save_model(path)
    print(f"saved {path}")


def main():
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("negatives")
    subparsers.add_parser("serp")
    train_parser = subparsers.add_parser("train")
    train_parser.add_argument("--no-domain", action="store_true")
    args = parser.parse_args()
    if args.command == "negatives":
        build_negatives()
    elif args.command == "serp":
        build_serp_domains()
    else:
        train(use_domain=not args.no_domain)


if __name__ == "__main__":
    main()
