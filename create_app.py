import re
from logging import getLogger
from typing import List

import Levenshtein
from fastapi import FastAPI
from starlette.responses import RedirectResponse, FileResponse, HTMLResponse
from starlette.staticfiles import StaticFiles

from index import TinyIndex, Document


logger = getLogger(__name__)


def create(tiny_index: TinyIndex):
    app = FastAPI()

    @app.get("/search")
    def search(s: str):
        results, terms = get_results(s)

        formatted_results = []
        for result in results:
            term_patterns = [rf'\b{term}\b' for term in terms]
            pattern = '|'.join(term_patterns)
            title = result.title
            matches = re.finditer(pattern, title, re.IGNORECASE)
            all_spans = [0] + sum((list(m.span()) for m in matches), []) + [len(title)]
            formatted_result = []
            for i in range(len(all_spans) - 1):
                is_bold = i % 2 == 1
                start = all_spans[i]
                end = all_spans[i + 1]
                formatted_result.append({'value': title[start:end], 'is_bold': is_bold})
            formatted_results.append({'title': formatted_result, 'url': result.url})

        logger.info("Return results: %r", formatted_results)
        return formatted_results

    def order_results(query, results: List[Document]):
        ordered_results = sorted(results, key=lambda result: Levenshtein.distance(query, result.title))
        # print("Order results", query, ordered_results, sep='\n')
        return ordered_results

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

    def get_results(q):
        terms = [x.lower() for x in q.replace('.', ' ').split()]
        # completed = complete_term(terms[-1])
        # terms = terms[:-1] + [completed]
        pages = []
        for term in terms:
            items = tiny_index.retrieve(term)
            if items is not None:
                pages += [item for item in items if term in item.title.lower()]
        ordered_results = order_results(q, pages)
        return ordered_results, terms

    @app.get('/')
    def index():
        return FileResponse('static/index.html')

    app.mount('/', StaticFiles(directory="static"), name="static")
    return app
