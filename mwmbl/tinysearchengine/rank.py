import html
import json
import math
import re
import urllib
from abc import abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from logging import getLogger
from operator import itemgetter
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter, Retry

from mwmbl.format import get_query_regex
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.indexer.external_cache import get_cached_external_results, store_external_results
from mwmbl.indexer.purge_queue import enqueue_for_purge
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, DocumentState, TinyIndex
from mwmbl.tokenizer import get_bigrams, tokenize
from mwmbl.utils import get_domain

logger = getLogger(__name__)


MAX_QUERY_CHARS = 100
MATCH_SCORE_THRESHOLD = 0.0
SCORE_THRESHOLD = 0.0
LENGTH_PENALTY = 0.04
MATCH_EXPONENT = 2
DOMAIN_SCORE_SMOOTHING = 0.1
HTTPS_STRING = "https://"
WIKI_SCORES = json.load(open(Path(__file__).parent.parent / "resources" / "wiki_stats.json"))
WIKI_MAX_SCORE = next(iter(WIKI_SCORES.values()))
DOCUMENT_FREQUENCIES = json.load(open(Path(__file__).parent.parent / "resources" / "document_counts.json"))
N_DOCUMENTS = max(DOCUMENT_FREQUENCIES.values())


def score_result(terms: list[str], result: Document, is_complete: bool):
    features = get_features(terms, result.title, result.url, result.extract, result.score, is_complete)

    length_penalty = math.e ** (-LENGTH_PENALTY * len(result.url))
    match_score = (
        4 * features["match_score_title"]
        + features["match_score_extract"]
        + 2 * features["match_score_domain"]
        + 2 * features["match_score_domain_tokenized"]
        + features["match_score_path"]
    )

    # Documents a person or an external source deliberately attached to a query are exempt
    # from the majority-terms rule: they are here because someone said they belong here, not
    # because they matched. That exemption was written as "state is not None" when every
    # state meant exactly that (FROM_USER, FROM_GOOGLE, FROM_WIKI). The negative states are
    # later bookkeeping - SYNCED_WITH_MAIN_INDEX marks a crawler's local mirror of the main
    # index - and nobody curated those, so they get the rule like anything else.
    is_curated = result.state is not None and result.state > 0
    if features["match_terms"] <= len(terms) / 2 and not is_curated:
        return 0.0

    if match_score > MATCH_SCORE_THRESHOLD:
        return match_score * length_penalty * (features["domain_score"] + DOMAIN_SCORE_SMOOTHING) / 10

    return 0.0


def score_result_whole(terms: list[str], result: Document, is_complete: bool) -> float:
    """Score a document using the combined whole-field match for promotion decisions.

    Uses match_score_whole from get_features() without the majority-terms zero filter,
    so partial matches score continuously rather than collapsing to 0.0.
    """
    if not result.url:
        return 0.0
    features = get_features(
        terms,
        result.title or "",
        result.url,
        result.extract or "",
        result.score or 0.0,
        is_complete,
    )
    length_penalty = math.e ** (-LENGTH_PENALTY * len(result.url))
    return features["match_score_whole"] * length_penalty * (features["domain_score"] + DOMAIN_SCORE_SMOOTHING) / 10


def score_match(last_match_char, match_length, total_possible_match_length):
    # return (match_length + 1. / last_match_char) / (total_possible_match_length + 1)
    return MATCH_EXPONENT ** (match_length - total_possible_match_length) / last_match_char


def get_tf_idf_features(match_counts: dict[str, int]) -> dict[str, float]:
    if len(match_counts) == 0:
        return {
            "max_tf_idf": 0.0,
            "min_tf_idf": 0.0,
            "mean_tf_idf": 0.0,
            "std_tf_idf": 0.0,
            "sum_tf_idf": 0.0,
            "max_tf": 0.0,
            "min_tf": 0.0,
            "mean_tf": 0.0,
            "std_tf": 0.0,
            "sum_tf": 0.0,
            "max_idf": 0.0,
            "min_idf": 0.0,
            "mean_idf": 0.0,
            "std_idf": 0.0,
            "sum_idf": 0.0,
        }

    inv_dfs = np.array([math.log(N_DOCUMENTS / DOCUMENT_FREQUENCIES.get(term, 1)) for term in match_counts])
    tfs = np.array(list(match_counts.values()))
    tf_idfs = tfs * inv_dfs
    features = {
        "max_tf_idf": np.max(tf_idfs),
        "min_tf_idf": np.min(tf_idfs),
        "mean_tf_idf": np.mean(tf_idfs),
        "std_tf_idf": np.std(tf_idfs),
        "sum_tf_idf": np.sum(tf_idfs),
        "max_tf": np.max(tfs),
        "min_tf": np.min(tfs),
        "mean_tf": np.mean(tfs),
        "std_tf": np.std(tfs),
        "sum_tf": np.sum(tfs),
        "max_idf": np.max(inv_dfs),
        "min_idf": np.min(inv_dfs),
        "mean_idf": np.mean(inv_dfs),
        "std_idf": np.std(inv_dfs),
        "sum_idf": np.sum(inv_dfs),
    }

    return features


def get_features(terms, title, url, extract, score, is_complete):
    assert len(terms) > 0
    assert url is not None
    assert title is not None
    assert extract is not None
    features = {}
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    path = parsed_url.path
    query = parsed_url.query
    whole = title + " " + extract + " " + domain + " " + path + " " + query
    for part, name, is_url in [
        (title, "title", False),
        (extract, "extract", False),
        (domain, "domain", True),
        (domain, "domain_tokenized", False),
        (path, "path", True),
        (query, "query", False),
        (whole, "whole", False),
    ]:
        last_match_char, match_length, total_possible_match_length, match_terms, match_counts = get_match_features(
            terms, part, is_complete, is_url
        )
        features[f"last_match_char_{name}"] = last_match_char
        features[f"match_length_{name}"] = match_length
        features[f"total_possible_match_length_{name}"] = total_possible_match_length
        features[f"match_score_{name}"] = score_match(last_match_char, match_length, total_possible_match_length)
        features[f"match_terms_{name}"] = match_terms
        features[f"match_term_proportion_{name}"] = match_terms / len(terms)

        # tf_idf_features = get_tf_idf_features(match_counts)
        # features.update({f"{name}_{k}": v for k, v in tf_idf_features.items()})

    features["num_terms"] = len(terms)
    features["num_chars"] = len(" ".join(terms))
    features["domain_score"] = get_domain_score(url)
    features["path_length"] = len(path)
    features["domain_length"] = len(domain)
    features["wiki_score"] = get_wiki_score(url)
    features["item_score"] = score
    features["match_terms"] = max(
        features[f"match_terms_{name}"] for name in ["title", "extract", "domain", "domain_tokenized", "path"]
    )

    return features


DOMAIN_MAX_SCORE = max(DOMAINS.values())
DOMAIN_MIN_SCORE = min(DOMAINS.values())


def get_domain_score(url):
    domain = urlparse(url).netloc

    if domain in DOMAINS:
        normalised_score = (DOMAINS[domain] - DOMAIN_MIN_SCORE) / (DOMAIN_MAX_SCORE - DOMAIN_MIN_SCORE)
        return normalised_score

    return 0.0


def get_match_features(terms, result_string, is_complete, is_url):
    query_regex = get_query_regex(terms, is_complete, is_url)
    matches = list(re.finditer(query_regex, result_string, flags=re.IGNORECASE))
    # match_strings = {x.group(0).lower() for x in matches}
    # match_length = sum(len(x) for x in match_strings)

    last_match_char = 1
    seen_matches = set()
    match_length = 0
    match_counts = defaultdict(int)
    for match in matches:
        value = match.group(0).lower()
        match_counts[value] += 1
        if value not in seen_matches:
            last_match_char = match.span()[1]
            seen_matches.add(value)
            match_length += len(value)

    total_possible_match_length = sum(len(x) for x in terms)
    return last_match_char, match_length, total_possible_match_length, len(seen_matches), match_counts


def get_wiki_score(url):
    title = url.split("/")[-1]
    return WIKI_SCORES.get(title, 0.0) / WIKI_MAX_SCORE


def find_blacklisted_urls(documents: list[Document]) -> set[str]:
    """URLs among these documents whose domain is blacklisted.

    Enforcing the blacklist here, on the read path, is what actually guarantees a
    blacklisted domain is never shown. The write-path checks (crawl assignment, link
    discovery, index_documents) only stop *new* content: they do nothing about the
    documents already in the index from before a domain was blacklisted, and the index is
    far too large to sweep on demand. Filtering at retrieval makes a newly blacklisted
    domain disappear from results immediately, and the purge queue then cleans up the
    index in the background so the work is not repeated forever.

    Cost is ~94us for a typical query's ~200 distinct domains - see blacklist_snapshot.
    """
    domains_to_urls = defaultdict(set)
    for document in documents:
        try:
            domain = get_domain(document.url)
        except ValueError:
            # Unparseable URL - keep it, matching index_batches.filter_blacklisted_documents.
            continue
        domains_to_urls[domain].add(document.url)

    blacklisted_domains = get_snapshot_blacklist().filter_blacklisted(domains_to_urls)
    return {url for domain in blacklisted_domains for url in domains_to_urls[domain]}


def deduplicate(results, seen_titles):
    deduplicated_results = []
    for result in results:
        if result.title not in seen_titles:
            deduplicated_results.append(result)
            seen_titles.add(result.title)
    return deduplicated_results


def fix_document_state(result: Document):

    try:
        fixed_state = DocumentState(result.state)
    except ValueError:
        fixed_state = None
    # `source` is carried across: it is what tells the formatter a result came from
    # Wikipedia or Staan rather than our index, and rebuilding without it silently
    # relabelled every external result as "mwmbl". user_ids and last_crawled are still
    # dropped on purpose - the formatted path has no business exposing them.
    fixed_document = Document(
        result.title,
        result.url,
        result.extract,
        result.score,
        result.term,
        fixed_state,
        source=result.source,
    )
    return fixed_document


def remove_curate_state(state: DocumentState):
    if state == DocumentState.ORGANIC_APPROVED:
        return None
    if state == DocumentState.FROM_USER_APPROVED:
        return DocumentState.FROM_USER
    if state == DocumentState.FROM_GOOGLE_APPROVED:
        return DocumentState.FROM_GOOGLE
    return state


@dataclass
class Retrieval:
    """What Ranker.retrieve found in the index for a query, ready to be ranked."""

    query: str
    terms: list[str]
    completions: list[str]
    is_complete: bool
    curated_items: list[Document]
    pages: list[Document]


class Ranker:
    def __init__(self, tiny_index: TinyIndex, completer: Completer):
        self.tiny_index = tiny_index
        self.completer = completer

    @abstractmethod
    def order_results(self, terms: list[str], pages: list[Document], is_complete: bool):
        pass

    def search(self, s: str, additional_results: list[Document], use_external_search: bool = True) -> list[Document]:
        # use_external_search=False is how the search-as-you-type path avoids a Wikipedia
        # call per keystroke; get_results has taken the flag all along, but only complete()
        # could reach it. See mwmbl.views.
        results, _, _ = self.get_results(s, additional_results, use_external_search)
        return self._drop_repeated_urls(results)

    def search_retrieved(self, retrieval: Retrieval, additional_results: list[Document]) -> list[Document]:
        """search() over an index retrieval already made with retrieve().

        This is how a caller overlaps the index lookup with a slow external fetch: retrieve
        and fetch concurrently, then rank the two together here. Never fetches Wikipedia.
        """
        results = self._rank(retrieval, additional_results, external_search_items=[])
        return self._drop_repeated_urls(results)

    @staticmethod
    def _drop_repeated_urls(results: list[Document]) -> list[Document]:
        ranked_results = []
        seen_urls = set()
        for result in results:
            if result.url in seen_urls:
                continue
            ranked_results.append(result)
            seen_urls.add(result.url)

        logger.info("Return results: %d", len(ranked_results))
        return ranked_results

    def complete(self, q: str):
        # Autocomplete fires on every keystroke, so it must never trigger external_search
        # (e.g. a live Wikipedia lookup) - that would multiply outbound requests by the
        # length of every query typed and can get us rate-limited (see fix-wiki-overuse).
        ordered_results, terms, completions = self.get_results(q, [], use_external_search=False)
        if len(ordered_results) == 0:
            # There are no results so suggest Google searches instead
            completion_queries = [" ".join(terms[:-1] + [t]) for t in completions]
            adjusted_completions = completion_queries if q in completion_queries else [q] + completion_queries
            completed = ["search: google.com " + t for t in adjusted_completions]
            return [q, completed]
        else:
            adjusted_completions = [c for c in completions if c != terms[-1]]

            urls = [
                "go: " + item.url[len(HTTPS_STRING) :].rstrip("/")
                for item in ordered_results[:5]
                if item.url.startswith(HTTPS_STRING) and all(term in item.url for term in terms)
            ][:1]
            completed = [" ".join(terms[:-1] + [t]) for t in adjusted_completions]
            return [q, urls + completed]

    def get_results(self, q: str, additional_results: list[Document], use_external_search: bool = True):
        logger.info(f"Get results with {len(additional_results)} additional results")
        retrieval = self.retrieve(q)
        external_search_items = self.external_search(q) if use_external_search else []
        results = self._rank(retrieval, additional_results, external_search_items)
        return results, retrieval.terms, retrieval.completions

    def retrieve(self, q: str) -> Retrieval:
        """The index lookup for a query: everything get_results needs before ranking."""
        terms = tokenize(q)

        is_complete = q.endswith(" ")
        if len(terms) > 0 and not is_complete:
            completions = self.completer.complete(terms[-1])
            retrieval_terms = set(terms + completions)
        else:
            completions = []
            retrieval_terms = set(terms)

        # Check for curation
        curation_term = " ".join(terms)
        curation_items = self.tiny_index.retrieve(curation_term)
        curated_items = [d for d in curation_items if d.state is not None and d.term == curation_term]

        bigrams = set(get_bigrams(len(terms), terms))

        pages = []
        for term in retrieval_terms | bigrams:
            # An optimisation - we have already retrieved this, so make use of it
            if term == curation_term:
                items = curation_items
            else:
                items_wrong_state = self.tiny_index.retrieve(term)
                # If this is not a curation term, it is not curated for the current term
                items = [
                    Document(
                        result.title,
                        result.url,
                        result.extract,
                        result.score,
                        result.term,
                        remove_curate_state(result.state),
                        source=result.source,
                    )
                    for result in items_wrong_state
                ]

            if items is not None:
                pages += items

        return Retrieval(q, terms, completions, is_complete, curated_items, pages)

    def _rank(
        self, retrieval: Retrieval, additional_results: list[Document], external_search_items: list[Document]
    ) -> list[Document]:
        candidates = retrieval.pages + additional_results + external_search_items
        candidates, curated_items = self._remove_blacklisted(
            candidates, retrieval.curated_items, index_items=retrieval.pages
        )

        ordered_results = self.order_results(retrieval.terms, candidates, retrieval.is_complete)
        deduplicated_results = deduplicate(curated_items + ordered_results, set())
        return [fix_document_state(result) for result in deduplicated_results]

    @staticmethod
    def _remove_blacklisted(
        candidates: list[Document], curated_items: list[Document], index_items: list[Document]
    ) -> tuple[list[Document], list[Document]]:
        """Drop blacklisted documents from a result set and queue the indexed ones for purging.

        Curated items are filtered separately because they bypass order_results entirely -
        they are prepended straight into deduplicate() - so filtering only `candidates`
        would leave a hole for any blacklisted domain that had been curated for a term.

        Only index-sourced documents go on the purge queue: there is nothing to purge for
        a Wikipedia or Super Search result, and nothing to purge for the caller-supplied
        additional_results either.
        """
        if not settings.BLACKLIST_FILTER_AT_RETRIEVAL:
            return candidates, curated_items

        blacklisted_urls = find_blacklisted_urls(candidates + curated_items)
        if not blacklisted_urls:
            return candidates, curated_items

        logger.info("Filtered %d blacklisted documents from results", len(blacklisted_urls))

        enqueue_for_purge([d for d in index_items if d.url in blacklisted_urls])

        return (
            [d for d in candidates if d.url not in blacklisted_urls],
            [d for d in curated_items if d.url not in blacklisted_urls],
        )

    def external_search(self, q: str):
        return []

    def get_raw_results(self, query: str):
        tokens = tokenize(query)
        term = " ".join(tokens)
        results = self.tiny_index.retrieve(term)
        # /raw bypasses get_results(), so it needs its own filter or it is a way to read
        # blacklisted documents straight out of the index.
        filtered, _ = self._remove_blacklisted(results, [], index_items=results)
        return filtered


class HeuristicRanker(Ranker):
    def __init__(self, tiny_index: TinyIndex, completer: Completer, score_threshold: float = 0.0):
        super().__init__(tiny_index, completer)
        self.score_threshold = score_threshold

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        # wiki_results = [result for result in results if result.state == DocumentState.FROM_WIKI]
        # wiki_urls = {result.url for result in wiki_results}
        # other_results = [result for result in results if result.url not in wiki_urls]
        # results_and_scores = [(score_result(terms, result, is_complete), result) for result in other_results]
        # ordered_results = sorted(results_and_scores, key=itemgetter(0), reverse=True)
        # filtered_results = [result for score, result in ordered_results if score > self.score_threshold]
        # return wiki_results + filtered_results

        results_and_scores = [(score_result(terms, result, is_complete), result) for result in results]
        ordered_results = sorted(results_and_scores, key=itemgetter(0), reverse=True)
        filtered_results = [result for score, result in ordered_results if score > self.score_threshold]
        return filtered_results


WIKI_SEARCH_API_URL = (
    "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&srlimit={limit}&format=json"
)
WIKI_URL_FORMAT = "https://en.wikipedia.org/wiki/{title}"

# How many wiki results a caller gets unless it says otherwise, and the number the LTR
# model was trained against - mwmbl.rankeval.ltr.dataset builds its training data through
# HeuristicAndWikiRanker with this default. Serving used to pass 3 here while training used
# 5, so the model was scoring production pools it had never seen the shape of. One constant,
# used everywhere, is what keeps them from drifting apart again.
NUM_WIKI_RESULTS = 5

# What we ask Wikipedia for, and therefore what a cache entry holds. Fixed rather than the
# caller's max_wiki_results so that one entry serves every caller: a cache warmed by a
# caller wanting three must not be a short answer to a caller wanting five. Left unset the
# API returns 10 by default, so this narrows the response as well.
WIKI_FETCH_LIMIT = NUM_WIKI_RESULTS

# The score a wiki result carries into the LTR model, top result first, counting down.
#
# A function of rank alone, deliberately. It used to be max_wiki_results + 1 - i, so the
# same Wikipedia result scored 6 for a caller asking for five and 4 for a caller asking for
# three - and `score` is a feature the model reads, so the number a caller passed for how
# *many* results it wanted silently moved the ranking of the ones it got. Anchored at the
# top instead, rank 0 is 6.0 for everybody, which is what the deployed model was trained
# on (max_wiki_results=5 there). Changing it changes a trained-on feature: retrain.
WIKI_TOP_SCORE = 6.0

# Live search does not retry Wikipedia. It used to retry 429s, honouring Retry-After, on
# the theory that live search never bursts - but it does, and in October 2026 Wikipedia
# answered about three calls in four with a 429 and a Retry-After of tens of seconds. Each
# such search slept through every retry, over a minute in all, and since sync views share
# one thread per worker, every request queued behind it waited too: searches, page loads,
# and the token refreshes whose failure logs users out.
#
# Evaluation runs can afford to wait, and need the results they would otherwise drop, so
# they opt into this with wait_out_rate_limits.
WIKI_RETRY = Retry(
    total=4,
    backoff_factor=0.5,
    status_forcelist=(429, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
)

WIKI_TIMEOUT_SECONDS = 5

# A 429 pauses every live Wikipedia call for as long as Wikipedia asks. Held in Redis rather
# than per process, so that one worker's 429 spares the others from collecting their own.
WIKI_CIRCUIT_KEY = "wiki-circuit-open"
WIKI_CIRCUIT_DEFAULT_SECONDS = 60
# A Retry-After this long is more likely a mistake than a request; don't let one switch
# Wikipedia off for the day.
WIKI_CIRCUIT_MAX_SECONDS = 600


def _wiki_circuit_open() -> bool:
    return cache.get(WIKI_CIRCUIT_KEY) is not None


def _trip_wiki_circuit(seconds: int):
    cache.set(WIKI_CIRCUIT_KEY, 1, timeout=seconds)


def _wiki_cooldown_seconds(response: requests.Response) -> int:
    """How long to pause for, from the 429's Retry-After.

    Retry-After may also be an HTTP date; Wikipedia sends seconds, so anything else gets
    the default rather than a date parser.
    """
    retry_after = response.headers.get("Retry-After", "")
    seconds = int(retry_after) if retry_after.isdigit() else WIKI_CIRCUIT_DEFAULT_SECONDS
    return min(max(seconds, 1), WIKI_CIRCUIT_MAX_SECONDS)


def get_wiki_url(title: str):
    return WIKI_URL_FORMAT.format(title=title.replace(" ", "_"))


HTML_TAG_REGEX = re.compile(r"<[^>]+>")


def clean_html(s: str):
    return html.unescape(HTML_TAG_REGEX.sub("", s))


def wiki_score(rank: int) -> float:
    """The `score` feature for a wiki result at 0-based `rank`. See WIKI_TOP_SCORE."""
    return WIKI_TOP_SCORE - rank


def get_wiki_results(
    s: str, max_wiki_results: int = NUM_WIKI_RESULTS, *, wait_out_rate_limits: bool = False
) -> list[Document]:
    # MAX_QUERY_CHARS was only ever applied in HeuristicAndWikiRanker, which is the eval
    # ranker - the production LTRRanker path sent the untruncated query straight out.
    query = s[:MAX_QUERY_CHARS]

    # Ahead of the circuit breaker deliberately: a cache hit costs Wikipedia nothing, so it
    # should still be served while we are backing off from a rate limit.
    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, query)
    if cached is not None:
        # The cache stores the rank Wikipedia gave, not a score - scoring is this function's
        # to do, and doing it here rather than at write time is what makes an entry mean the
        # same thing to every caller. The documents are ours; nothing else holds them.
        wiki_results = cached[:max_wiki_results]
        for rank, document in enumerate(wiki_results):
            document.score = wiki_score(rank)
        return wiki_results

    if not wait_out_rate_limits and _wiki_circuit_open():
        return []

    escaped_query = urllib.parse.quote(query, safe="")
    headers = {"User-Agent": "Mwmbl/0.1.0 (https://mwmbl.org; daoud@mwmbl.org)"}
    try:
        with requests.Session() as session:
            if wait_out_rate_limits:
                session.mount("https://en.wikipedia.org", HTTPAdapter(max_retries=WIKI_RETRY))
            response = session.get(
                WIKI_SEARCH_API_URL.format(query=escaped_query, limit=WIKI_FETCH_LIMIT),
                headers=headers,
                timeout=WIKI_TIMEOUT_SECONDS,
            )
            if response.status_code == 429:
                cooldown_seconds = _wiki_cooldown_seconds(response)
                _trip_wiki_circuit(cooldown_seconds)
                logger.warning("Wikipedia is rate-limiting us; pausing wiki lookups for %ds", cooldown_seconds)
                return []
            wiki_response = response.json()
    except Exception as e:
        logger.warning("Failed to fetch Wikipedia results: %s", type(e).__name__)
        return []

    if "query" not in wiki_response or "search" not in wiki_response["query"]:
        if "error" in wiki_response:
            # Don't log the error body - it can echo back the (private) user query.
            logger.warning("Wikipedia API returned an error response")

        return []

    wiki_results = [
        Document(
            result["title"],
            get_wiki_url(result["title"]),
            clean_html(result["snippet"]),
            wiki_score(rank),
            query,
            state=DocumentState.FROM_WIKI,
            source=DocumentSource.WIKIPEDIA,
        )
        for rank, result in enumerate(wiki_response["query"]["search"][:WIKI_FETCH_LIMIT])
    ]

    # Only a well-formed answer is stored, including one with no results in it. Every path
    # above returns [] without storing, so a transient failure is never remembered as
    # "Wikipedia has nothing for this".
    #
    # The whole response is stored and the caller's cut taken afterwards, so that a caller
    # wanting fewer than WIKI_FETCH_LIMIT does not leave a short entry behind for the next
    # caller to be silently short-changed by.
    store_external_results(DocumentSource.WIKIPEDIA, query, wiki_results)
    return wiki_results[:max_wiki_results]


class HeuristicAndWikiRanker(HeuristicRanker):
    def __init__(
        self,
        tiny_index: TinyIndex,
        completer: Completer,
        return_none_if_no_mwmbl_results: bool = False,
        score_threshold: float = 0.0,
        max_wiki_results: int = NUM_WIKI_RESULTS,
    ):
        super().__init__(tiny_index, completer, score_threshold)
        self.return_none_if_no_mwmbl_results = return_none_if_no_mwmbl_results
        self.max_wiki_results = max_wiki_results

    def search(self, s: str, additional_results: list[Document], use_external_search: bool = True) -> list[Document]:
        s_shortened = s[:MAX_QUERY_CHARS]

        wiki_results = get_wiki_results(s_shortened, self.max_wiki_results) if use_external_search else []
        results = super().search(s_shortened, additional_results=wiki_results)

        if len(results) == 0 and self.return_none_if_no_mwmbl_results:
            return []

        return results
