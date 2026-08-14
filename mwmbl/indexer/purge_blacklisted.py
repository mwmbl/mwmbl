"""Shared logic for finding and removing already-indexed documents on blacklisted domains.

Used by both the purge_blacklisted_domains management command and the admin
"Purge blacklisted domains" tool. See purge_blacklisted_domains.py for the full
explanation of targeted vs. full-scan purging.
"""
import csv
from dataclasses import dataclass
from typing import Callable, Iterable

from mwmbl.indexer.index import get_index_tokens, prepare_url_for_tokenizing, tokenize_document
from mwmbl.tinysearchengine.indexer import TinyIndex
from mwmbl.tokenizer import get_bigrams, tokenize
from mwmbl.utils import get_domain


@dataclass
class MatchedDocument:
    """A blacklisted-domain document found via a targeted scan."""
    domain: str
    title: str
    url: str
    found_via_terms: list[str]       # seed terms whose page contained this document
    indexed_under_terms: list[str]   # this document's full recomputed token set - every
                                      # page it's actually filed under, and so every page
                                      # it will be removed from


def guess_terms_for_domain(domain: str) -> set[str]:
    """Terms the indexer would have derived from this domain appearing in a URL."""
    url_tokens = tokenize(prepare_url_for_tokenizing(f"https://{domain}/"))
    return get_index_tokens(url_tokens)


def seed_terms_for_query(query: str) -> set[str]:
    """The terms real search retrieval would look up for this query (see Ranker.get_results)."""
    terms = tokenize(query)
    bigrams = set(get_bigrams(len(terms), terms))
    return set(terms) | bigrams


def load_queries_from_csv(csv_path: str) -> list[str]:
    """Read a Search Console query-export CSV: first column is the query, header row skipped."""
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    return [row[0] for row in rows[1:] if row and row[0].strip()]


def parse_pasted_queries(text: str) -> list[str]:
    """Parse queries pasted into a text box: one per line, tolerating pasted CSV rows
    (query,clicks,impressions,...) by taking the text before the first comma, and
    skipping a Search Console header row if present."""
    queries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        query = line.split(',')[0].strip().strip('"')
        if not query or query.lower() == "top queries":
            continue
        queries.append(query)
    return queries


def _split_page(documents, is_blacklisted: Callable[[str], bool]):
    """Partition a page's documents into (kept, removed) by domain."""
    kept, removed = [], []
    for document in documents:
        try:
            domain = get_domain(document.url)
        except ValueError:
            kept.append(document)
            continue
        (removed if is_blacklisted(domain) else kept).append(document)
    return kept, removed


def discover_targeted_matches(
        index: TinyIndex, seed_terms: set[str], is_blacklisted: Callable[[str], bool]) -> list[MatchedDocument]:
    """Find every blacklisted-domain document reachable from the seed terms, and for each
    one recompute its exact token set (tokenize_document is a pure function of its stored
    url/title/extract) to discover every page it's actually filed under."""
    page_terms: dict[int, list[str]] = {}
    for term in seed_terms:
        page_terms.setdefault(index.get_key_page_index(term), []).append(term)

    matches: dict[str, MatchedDocument] = {}
    for page_index, terms_for_page in page_terms.items():
        for document in index.get_page(page_index):
            if document.url in matches:
                existing = matches[document.url].found_via_terms
                for term in terms_for_page:
                    if term not in existing:
                        existing.append(term)
                continue
            try:
                domain = get_domain(document.url)
            except ValueError:
                continue
            if not is_blacklisted(domain):
                continue
            tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
            matches[document.url] = MatchedDocument(
                domain=domain, title=document.title, url=document.url,
                found_via_terms=list(terms_for_page), indexed_under_terms=sorted(tokenized.tokens))

    return list(matches.values())


def candidate_pages_for_matches(index: TinyIndex, matches: Iterable[MatchedDocument]) -> set[int]:
    pages = set()
    for match in matches:
        pages.update(index.get_key_page_index(term) for term in match.indexed_under_terms)
    return pages


def purge_pages(index: TinyIndex, page_indexes: Iterable[int], is_blacklisted: Callable[[str], bool],
                 dry_run: bool, on_progress: Callable[[int, dict], None] | None = None):
    """Scan the given pages, removing (unless dry_run) any document whose domain is
    blacklisted. Returns (removed_by_domain, pages_changed, pages_scanned)."""
    removed_by_domain: dict[str, int] = {}
    pages_changed = 0
    pages_scanned = 0

    for page_index in page_indexes:
        pages_scanned += 1
        kept, removed = _split_page(index.get_page(page_index), is_blacklisted)

        if removed:
            pages_changed += 1
            for document in removed:
                domain = get_domain(document.url)
                removed_by_domain[domain] = removed_by_domain.get(domain, 0) + 1
            if not dry_run:
                index.store_in_page(page_index, kept)

        if on_progress:
            on_progress(page_index, removed_by_domain)

    return removed_by_domain, pages_changed, pages_scanned


def purge_targeted(index: TinyIndex, seed_terms: set[str], is_blacklisted: Callable[[str], bool], dry_run: bool):
    """Full targeted purge: discover matches from seed terms, then purge every page
    they're filed under. Returns (matches, removed_by_domain, pages_changed, pages_scanned)."""
    matches = discover_targeted_matches(index, seed_terms, is_blacklisted)
    candidate_pages = candidate_pages_for_matches(index, matches) | {
        index.get_key_page_index(term) for term in seed_terms}
    removed_by_domain, pages_changed, pages_scanned = purge_pages(index, candidate_pages, is_blacklisted, dry_run)
    return matches, removed_by_domain, pages_changed, pages_scanned
