import math
import re
from abc import abstractmethod
from logging import getLogger
from operator import itemgetter
from typing import Optional
from urllib.parse import urlparse

from mwmbl.format import format_result_with_pattern, get_query_regex
from mwmbl.tokenizer import tokenize, get_bigrams
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, DocumentState

logger = getLogger(__name__)


MATCH_SCORE_THRESHOLD = 0.0
SCORE_THRESHOLD = 0.0
LENGTH_PENALTY = 0.04
MATCH_EXPONENT = 2
DOMAIN_SCORE_SMOOTHING = 50
HTTPS_STRING = 'https://'


def score_result(terms: list[str], result: Document, is_complete: bool):
    features = get_features(terms, result.title, result.url, result.extract, result.score, is_complete)

    length_penalty = math.e ** (-LENGTH_PENALTY * len(result.url))
    match_score = (4 * features['match_score_title'] + features['match_score_extract'] + 2 * features[
        'match_score_domain'] + 2 * features['match_score_domain_tokenized'] + features['match_score_path'])

    max_match_terms = max(features[f'match_terms_{name}']
                          for name in ['title', 'extract', 'domain', 'domain_tokenized', 'path'])
    if max_match_terms <= len(terms) / 2 and result.state is None:
        return 0.0

    if match_score > MATCH_SCORE_THRESHOLD:
        return match_score * length_penalty * (features['domain_score'] + DOMAIN_SCORE_SMOOTHING) / 10

    # best_match_score = max(features[f'match_score_{name}'] for name in ['title', 'extract', 'domain', 'domain_tokenized'])
    # score = best_match_score * length_penalty * (features['domain_score'] + DOMAIN_SCORE_SMOOTHING)
    return 0.0


def score_match(last_match_char, match_length, total_possible_match_length):
    # return (match_length + 1. / last_match_char) / (total_possible_match_length + 1)
    return MATCH_EXPONENT ** (match_length - total_possible_match_length) / last_match_char


def get_features(terms, title, url, extract, score, is_complete):
    assert url is not None
    assert title is not None
    assert extract is not None
    features = {}
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    path = parsed_url.path
    for part, name, is_url in [(title, 'title', False),
                               (extract, 'extract', False),
                               (domain, 'domain', True),
                               (domain, 'domain_tokenized', False),
                               (path, 'path', True)]:
        last_match_char, match_length, total_possible_match_length, match_terms = \
            get_match_features(terms, part, is_complete, is_url)
        features[f'last_match_char_{name}'] = last_match_char
        features[f'match_length_{name}'] = match_length
        features[f'total_possible_match_length_{name}'] = total_possible_match_length
        features[f'match_score_{name}'] = score_match(last_match_char, match_length, total_possible_match_length)
        features[f'match_terms_{name}'] = match_terms
    features['num_terms'] = len(terms)
    features['num_chars'] = len(' '.join(terms))
    features['domain_score'] = get_domain_score(url)
    features['path_length'] = len(path)
    features['domain_length'] = len(domain)
    features['item_score'] = score
    return features


def get_domain_score(url):
    domain = urlparse(url).netloc
    domain_score = DOMAINS.get(domain, 0.0)
    return domain_score


def get_match_features(terms, result_string, is_complete, is_url):
    query_regex = get_query_regex(terms, is_complete, is_url)
    matches = list(re.finditer(query_regex, result_string, flags=re.IGNORECASE))
    # match_strings = {x.group(0).lower() for x in matches}
    # match_length = sum(len(x) for x in match_strings)

    last_match_char = 1
    seen_matches = set()
    match_length = 0
    for match in matches:
        value = match.group(0).lower()
        if value not in seen_matches:
            last_match_char = match.span()[1]
            seen_matches.add(value)
            match_length += len(value)

    total_possible_match_length = sum(len(x) for x in terms)
    return last_match_char, match_length, total_possible_match_length, len(seen_matches)


def order_results(terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
    if len(results) == 0:
        return []

    results_and_scores = [(score_result(terms, result, is_complete), result) for result in results]
    ordered_results = sorted(results_and_scores, key=itemgetter(0), reverse=True)
    filtered_results = [result for score, result in ordered_results if score > SCORE_THRESHOLD]
    return filtered_results


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
    def order_results(self, terms, pages, is_complete):
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

        ordered_results = self.order_results(terms, pages + additional_results, is_complete)
        deduplicated_results = deduplicate(curated_items + ordered_results, set())
        state_fixed = [fix_document_state(result) for result in deduplicated_results]
        return state_fixed, terms, completions

    def get_raw_results(self, query: str):
        tokens = tokenize(query)
        term = ' '.join(tokens)
        return self.tiny_index.retrieve(term)


class HeuristicRanker(Ranker):
    def order_results(self, terms, pages, is_complete):
        return order_results(terms, pages, is_complete)
