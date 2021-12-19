import re
from logging import getLogger
from operator import itemgetter

from fastapi import FastAPI
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from index import TinyIndex, Document

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25


def create(tiny_index: TinyIndex):
    app = FastAPI()

    @app.get("/search")
    def search(s: str):
        results, terms = get_results(s)

        formatted_results = []
        for result in results:
            pattern = get_query_regex(terms)
            title_and_extract = f"{result.title} - {result.extract}"
            matches = re.finditer(pattern, title_and_extract, re.IGNORECASE)
            all_spans = [0] + sum((list(m.span()) for m in matches), []) + [len(title_and_extract)]
            formatted_result = []
            title_length = len(result.title)
            for i in range(len(all_spans) - 1):
                is_bold = i % 2 == 1
                start = all_spans[i]
                end = all_spans[i + 1]
                formatted_result.append({'value': title_and_extract[start:end], 'is_bold': is_bold})
            formatted_results.append({'title': formatted_result, 'url': result.url})

        logger.info("Return results: %r", formatted_results)
        return formatted_results

    def get_query_regex(terms):
        term_patterns = [rf'\b{term}\b' for term in terms]
        pattern = '|'.join(term_patterns)
        return pattern

    def score_result(terms, result: Document):
        print("Score result", result)
        result_string = f"{result.title} {result.extract}"
        query_regex = get_query_regex(terms)
        matches = re.findall(query_regex, result_string, flags=re.IGNORECASE)
        match_strings = {x.lower() for x in matches}
        match_length = sum(len(x) for x in match_strings)

        num_words = len(re.findall(r'\b\w+\b', result_string))
        total_possible_match_length = sum(len(x) for x in terms)
        return (match_length + 1./num_words) / (total_possible_match_length + 1)

    def order_results(terms: list[str], results: list[Document]):
        results_and_scores = [(score_result(terms, result), result) for result in results]
        ordered_results = sorted(results_and_scores, key=itemgetter(0), reverse=True)
        # print("Ordered results", ordered_results)
        filtered_results = [result for score, result in ordered_results if score > SCORE_THRESHOLD]
        return filtered_results

    @app.get("/complete")
    def complete(q: str):
        ordered_results, terms = get_results(q)
        results = [item.title.replace("\n", "") + ' — ' +
                   item.url.replace("\n", "") for item in ordered_results]
        if len(results) == 0:
            # print("No results")
            return []
        # print("Results", results)
        return [q, results]

    # TODO: why does 'leek and potato soup' result not get returned for 'potato soup' query?
    def get_results(q):
        terms = [x.lower() for x in q.replace('.', ' ').split()]
        # completed = complete_term(terms[-1])
        # terms = terms[:-1] + [completed]
        pages = []
        seen_items = set()
        for term in terms:
            items = tiny_index.retrieve(term)
            print("Items", items)
            if items is not None:
                for item in items:
                    if term in item.title.lower() or term in item.extract.lower():
                        if item.title not in seen_items:
                            pages.append(item)
                            seen_items.add(item.title)

        ordered_results = order_results(terms, pages)
        return ordered_results, terms

    @app.get('/')
    def index():
        return FileResponse('static/index.html')

    app.mount('/', StaticFiles(directory="static"), name="static")
    return app
