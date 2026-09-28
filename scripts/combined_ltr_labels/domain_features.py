"""Query-independent features of a result's host, for the Combined Search domain-quality
experiment (`domain_experiment.py`).

The shared features carry one domain signal, `domain_score`: the host's rank in the HN top
domains list, 0 for the 7.9k hosts off it. These add what the write-time crawl model used
(`scripts/index_write_order/crawl_model.py` on the crawl-page-model branch, PR #463):

- `curated`: the host, or its registered domain, is a moderator-approved domain;
- `serp_queries_host`, `serp_top3_host`, `serp_queries_apex`: how many of the Firefox
  extension's Google SERPs the host appears in (in the top three; or its registered domain
  does). The en-gb eval queries are not in the table at all (`serp_domains.json`), and each
  row's own query is left out, so that a SERP-presence label cannot see itself;
- the shape of the host: labels, digits, hyphens, a numbered first label;

and two from the raw crawl (`devdata/index_write_order/crawl/`, `run_crawl.py`'s output):

- `crawl_pages`: how many pages of the host were crawled;
- `crawl_inlink_hosts`: how many other registered domains link to the host from the pages
  crawled. The crawl is small and skewed (about 9.5k source hosts), so this is a weak signal;

and four from Common Crawl's Jul–Sep 2026 web graph (`cc-main-2026-jul-aug-sep`), each graph
cut to its top 5M nodes by either measure (`cc_*_ranks.tsv`), missing (NaN) below that:

- `cc_host_hc_pos`, `cc_host_pr_pos`: the host's harmonic centrality and PageRank positions
  in the host-level graph;
- `cc_domain_hc_pos`, `cc_domain_pr_pos`: the same for its registered domain in the
  domain-level graph.

The SERP and curated tables come from the crawl-page-model branch's `crawl_model.py serp`
and the public curated-domains endpoint, in `devdata/index_write_order/`.
"""

import gzip
import json
import re
from collections import defaultdict
from functools import cache
from multiprocessing import Pool
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from mwmbl.tokenizer import tokenize

WRITE_ORDER = Path("devdata/index_write_order")
CURATED_PATH = WRITE_ORDER / "curated_domains.json"
SERP_DOMAINS_PATH = WRITE_ORDER / "serp_domains.json"
CRAWL_DIR = WRITE_ORDER / "crawl"
CRAWL_HOSTS_PATH = Path("devdata/combined_ltr_labels/crawl_hosts.json")
CC_RANKS_PATH = "devdata/combined_ltr_labels/cc_{level}_ranks.tsv"
PROCESSES = 8

# Registered domains one level below these public suffixes, e.g. bbc.co.uk.
SECOND_LEVEL_SUFFIXES = {"co", "ac", "gov", "org", "com", "net", "edu", "ne", "or", "go", "nhs", "ltd", "plc"}
NUMBERED_LABEL = re.compile(r"[a-z]\d{3,}|\d{3,}[a-z]")
TLDS = ["com", "org", "net", "uk", "edu", "gov", "io", "de", "fr", "info", "blog", "xyz", "online", "site"]

SERP_NAMES = ["curated", "serp_queries_host", "serp_top3_host", "serp_queries_apex"]
SHAPE_NAMES = [
    "host_labels",
    "host_digits",
    "host_hyphens",
    "first_label_length",
    "first_label_numbered",
    "is_www",
    "tld",
]
CRAWL_NAMES = ["crawl_pages", "crawl_inlink_hosts"]
RAW_NAMES = SERP_NAMES + SHAPE_NAMES
CC_NAMES = ["cc_host_hc_pos", "cc_host_pr_pos", "cc_domain_hc_pos", "cc_domain_pr_pos"]
ALL_NAMES = RAW_NAMES + CRAWL_NAMES + CC_NAMES


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def apex_of(host: str) -> str:
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in SECOND_LEVEL_SUFFIXES and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def normalise_query(query: str) -> str:
    return " ".join(tokenize(query))


@cache
def curated_domains() -> frozenset[str]:
    return frozenset(json.loads(CURATED_PATH.read_text()))


@cache
def serp_domains() -> tuple[dict[str, dict[str, int]], dict[str, set[str]]]:
    by_host = json.loads(SERP_DOMAINS_PATH.read_text())
    by_apex = defaultdict(set)
    for host, ranks in by_host.items():
        by_apex[apex_of(host)].update(ranks)
    return by_host, dict(by_apex)


def _crawl_batch_hosts(path: Path) -> list[tuple[str, list[str]]]:
    """(host, apexes it links to) per crawled page with content."""
    with gzip.open(path) as file:
        batch = json.load(file)
    pages = []
    for item in batch["items"]:
        content = item.get("content")
        if not content:
            continue
        host = host_of(item["url"])
        links = (content.get("links") or []) + (content.get("extra_links") or [])
        pages.append((host, sorted({apex_of(host_of(link)) for link in links})))
    return pages


def build_crawl_hosts() -> None:
    paths = sorted(CRAWL_DIR.glob("*/*.json.gz"))
    pages: dict[str, int] = defaultdict(int)
    inlinks: dict[str, set[str]] = defaultdict(set)
    with Pool(PROCESSES) as pool:
        for batch in pool.imap_unordered(_crawl_batch_hosts, paths, chunksize=16):
            for host, targets in batch:
                pages[host] += 1
                source = apex_of(host)
                for target in targets:
                    if target != source:
                        inlinks[target].add(source)
    table = {
        "pages": pages,
        "inlink_hosts": {apex: len(sources) for apex, sources in inlinks.items()},
    }
    CRAWL_HOSTS_PATH.write_text(json.dumps(table))
    print(f"{len(paths)} crawl files, {sum(pages.values())} pages, {len(pages)} hosts, {len(inlinks)} linked apexes")


@cache
def crawl_hosts() -> tuple[dict[str, int], dict[str, int]]:
    if not CRAWL_HOSTS_PATH.exists():
        build_crawl_hosts()
    table = json.loads(CRAWL_HOSTS_PATH.read_text())
    return table["pages"], table["inlink_hosts"]


@cache
def cc_ranks(level: str) -> dict[str, tuple[float, float]]:
    """Name -> (harmonic centrality position, PageRank position), names un-reversed."""
    ranks = pd.read_csv(
        CC_RANKS_PATH.format(level=level),
        sep="\t",
        header=None,
        usecols=[0, 2, 4],
        names=["hc_pos", "pr_pos", "name"],
        dtype={"hc_pos": np.float32, "pr_pos": np.float32, "name": str},
        keep_default_na=False,
    )
    names = [".".join(reversed(name.split("."))) for name in ranks["name"]]
    return dict(zip(names, zip(ranks["hc_pos"].tolist(), ranks["pr_pos"].tolist())))


def row_features(url: str, query: str | None) -> list[float]:
    """`query` is the row's own query, left out of the SERP counts."""
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
    pages, inlink_hosts = crawl_hosts()
    host_cc = cc_ranks("host").get(host, (np.nan, np.nan))
    domain_cc = cc_ranks("domain").get(apex, (np.nan, np.nan))
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
        float(TLDS.index(labels[-1]) if labels[-1] in TLDS else len(TLDS)),
        float(pages.get(host, 0)),
        float(inlink_hosts.get(apex, 0)),
        *host_cc,
        *domain_cc,
    ]


def domain_columns(frame: pd.DataFrame) -> np.ndarray:
    """ALL_NAMES for each row of a frame with `query` and `url` columns."""
    return np.array(
        [row_features(url, query) for query, url in zip(frame["query"], frame["url"])], dtype=np.float32
    ).reshape(len(frame), len(ALL_NAMES))


if __name__ == "__main__":
    build_crawl_hosts()
