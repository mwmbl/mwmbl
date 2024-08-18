import html
import json
import math
import re
import urllib
from abc import abstractmethod
from collections import defaultdict
from datetime import timedelta
from logging import getLogger
from operator import itemgetter
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from mwmbl.format import get_query_regex
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, DocumentState
from mwmbl.tokenizer import tokenize, get_bigrams
from mwmbl.utils import request_cache


logger = getLogger(__name__)


MAX_QUERY_CHARS = 100
MATCH_SCORE_THRESHOLD = 0.0
SCORE_THRESHOLD = 0.0
LENGTH_PENALTY = 0.04
MATCH_EXPONENT = 2
DOMAIN_SCORE_SMOOTHING = 0.1
HTTPS_STRING = 'https://'
WIKI_SCORES = json.load(open(Path(__file__).parent.parent / "resources" / "wiki_stats.json"))
WIKI_MAX_SCORE = next(iter(WIKI_SCORES.values()))
DOCUMENT_FREQUENCIES = json.load(open(Path(__file__).parent.parent / "resources" / "document_counts.json"))
N_DOCUMENTS = max(DOCUMENT_FREQUENCIES.values())


def score_result(terms: list[str], result: Document, is_complete: bool):
    features = get_features(terms, result.title, result.url, result.extract, result.score, is_complete)

    length_penalty = math.e ** (-LENGTH_PENALTY * len(result.url))
    match_score = (4 * features['match_score_title'] + features['match_score_extract'] + 2 * features[
        'match_score_domain'] + 2 * features['match_score_domain_tokenized'] + features['match_score_path'])

    if features[f'match_terms'] <= len(terms) / 2 and result.state is None:
        return 0.0

    if match_score > MATCH_SCORE_THRESHOLD:
        return match_score * length_penalty * (features['domain_score'] + DOMAIN_SCORE_SMOOTHING) / 10

    return 0.0


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
    assert url is not None
    assert title is not None
    assert extract is not None
    features = {}
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    path = parsed_url.path
    query = parsed_url.query
    whole = title + ' ' + extract + ' ' + domain + ' ' + path + ' ' + query
    for part, name, is_url in [(title, 'title', False),
                               (extract, 'extract', False),
                               (domain, 'domain', True),
                               (domain, 'domain_tokenized', False),
                               (path, 'path', True),
                               (query, 'query', False),
                               (whole, 'whole', False)]:
        last_match_char, match_length, total_possible_match_length, match_terms, match_counts = \
            get_match_features(terms, part, is_complete, is_url)
        features[f'last_match_char_{name}'] = last_match_char
        features[f'match_length_{name}'] = match_length
        features[f'total_possible_match_length_{name}'] = total_possible_match_length
        features[f'match_score_{name}'] = score_match(last_match_char, match_length, total_possible_match_length)
        features[f'match_terms_{name}'] = match_terms
        features[f'match_term_proportion_{name}'] = match_terms / len(terms)

        # tf_idf_features = get_tf_idf_features(match_counts)
        # features.update({f"{name}_{k}": v for k, v in tf_idf_features.items()})

    features['num_terms'] = len(terms)
    features['num_chars'] = len(' '.join(terms))
    features['domain_score'] = get_domain_score(url)
    features['path_length'] = len(path)
    features['domain_length'] = len(domain)
    features['wiki_score'] = get_wiki_score(url)
    features['item_score'] = score
    features['match_terms'] = max(features[f'match_terms_{name}']
                                  for name in ['title', 'extract', 'domain', 'domain_tokenized', 'path'])

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
    title = url.split('/')[-1]
    return WIKI_SCORES.get(title, 0.0) / WIKI_MAX_SCORE


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
    fixed_document = Document(result.title, result.url, result.extract, result.score, result.term, fixed_state)
    return fixed_document


def remove_curate_state(state: DocumentState):
    if state == DocumentState.ORGANIC_APPROVED:
        return None
    if state == DocumentState.FROM_USER_APPROVED:
        return DocumentState.FROM_USER
    if state == DocumentState.FROM_GOOGLE_APPROVED:
        return DocumentState.FROM_GOOGLE
    return state


class Ranker:
    def __init__(self, tiny_index: TinyIndex, completer: Completer):
        self.tiny_index = tiny_index
        self.completer = completer

    @abstractmethod
    def order_results(self, terms: list[str], pages: list[Document], is_complete: bool):
        pass

    def search(self, s: str, additional_results: list[Document]) -> list[Document]:
        results, terms, _ = self.get_results(s, additional_results)

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
        ordered_results, terms, completions = self.get_results(q, [])
        if len(ordered_results) == 0:
            # There are no results so suggest Google searches instead
            completion_queries = [' '.join(terms[:-1] + [t]) for t in completions]
            adjusted_completions = completion_queries if q in completion_queries else [q] + completion_queries
            completed = ["search: google.com " + t for t in adjusted_completions]
            return [q, completed]
        else:
            adjusted_completions = [c for c in completions if c != terms[-1]]

            urls = ["go: " + item.url[len(HTTPS_STRING):].rstrip('/') for item in ordered_results[:5]
                    if item.url.startswith(HTTPS_STRING) and all(term in item.url for term in terms)][:1]
            completed = [' '.join(terms[:-1] + [t]) for t in adjusted_completions]
            return [q, urls + completed]

    def get_results(self, q: str, additional_results: list[Document]):
        logger.info(f"Get results with {len(additional_results)} additional results")
        terms = tokenize(q)

        is_complete = q.endswith(' ')
        if len(terms) > 0 and not is_complete:
            completions = self.completer.complete(terms[-1])
            retrieval_terms = set(terms + completions)
        else:
            completions = []
            retrieval_terms = set(terms)

        # Check for curation
        curation_term = " ".join(terms)
        curation_items = self.tiny_index.retrieve(curation_term)
        curated_items = [d for d in curation_items if d.state is not None
                         and d.term == curation_term]

        bigrams = set(get_bigrams(len(terms), terms))

        pages = []
        for term in retrieval_terms | bigrams:
            # An optimisation - we have already retrieved this, so make use of it
            if term == curation_term:
                items = curation_items
            else:
                items_wrong_state = self.tiny_index.retrieve(term)
                # If this is not a curation term, it is not curated for the current term
                items = [Document(result.title,
                                  result.url,
                                  result.extract,
                                  result.score,
                                  result.term,
                                  remove_curate_state(result.state))
                         for result in items_wrong_state]

            if items is not None:
                pages += items

        external_search_items = self.external_search(q)
        ordered_results = self.order_results(terms, pages + additional_results + external_search_items, is_complete)
        deduplicated_results = deduplicate(curated_items + ordered_results, set())
        state_fixed = [fix_document_state(result) for result in deduplicated_results]
        return state_fixed, terms, completions

    def external_search(self, q: str):
        return []

    def get_raw_results(self, query: str):
        tokens = tokenize(query)
        term = ' '.join(tokens)
        return self.tiny_index.retrieve(term)


class HeuristicRanker(Ranker):
    def __init__(self, tiny_index: TinyIndex, completer: Completer, score_threshold: float = 0.0):
        super().__init__(tiny_index, completer)
        self.score_threshold = score_threshold

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        wiki_results = [result for result in results if result.state == DocumentState.FROM_WIKI]
        wiki_urls = {result.url for result in wiki_results}
        other_results = [result for result in results if result.url not in wiki_urls]
        results_and_scores = [(score_result(terms, result, is_complete), result) for result in other_results]
        ordered_results = sorted(results_and_scores, key=itemgetter(0), reverse=True)
        filtered_results = [result for score, result in ordered_results if score > self.score_threshold]
        return wiki_results + filtered_results


WIKI_SEARCH_API_URL = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json"
WIKI_URL_FORMAT = "https://en.wikipedia.org/wiki/{title}"


def get_wiki_url(title: str):
    return WIKI_URL_FORMAT.format(title=title.replace(" ", "_"))


HTML_TAG_REGEX = re.compile(r'<[^>]+>')


def clean_html(s: str):
    return html.unescape(HTML_TAG_REGEX.sub('', s))


def get_wiki_results(s: str, max_wiki_results: int) -> list[Document]:
    escaped_query = urllib.parse.quote(s, safe='')
    with request_cache(timedelta(weeks=10)) as session:
        wiki_response = session.get(WIKI_SEARCH_API_URL.format(query=escaped_query)).json()

    if 'query' not in wiki_response or 'search' not in wiki_response['query']:
        if 'error' in wiki_response:
            logger.warning("Error in wiki response: %s", wiki_response['error'])

        return []

    wiki_results = [Document(result['title'], get_wiki_url(result['title']), clean_html(result['snippet']),
                             max_wiki_results + 1 - i, s, state=DocumentState.FROM_WIKI)
                    for i, result in enumerate(wiki_response['query']['search'][:max_wiki_results])]
    return wiki_results


class HeuristicAndWikiRanker(HeuristicRanker):
    def __init__(
            self,
            tiny_index: TinyIndex,
            completer: Completer,
            return_none_if_no_mwmbl_results: bool = False,
            score_threshold: float = 0.0,
            max_wiki_results: int = 5
    ):
        super().__init__(tiny_index, completer, score_threshold)
        self.return_none_if_no_mwmbl_results = return_none_if_no_mwmbl_results
        self.max_wiki_results = max_wiki_results

    def search(self, s: str, additional_results: list[Document]) -> list[Document]:
        s_shortened = s[:MAX_QUERY_CHARS]

        results = super().search(s_shortened, additional_results)

        if len(results) == 0 and self.return_none_if_no_mwmbl_results:
            return []

        wiki_results = get_wiki_results(s_shortened, self.max_wiki_results)

        return wiki_results + results


