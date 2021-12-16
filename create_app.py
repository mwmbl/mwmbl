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
        results = get_results(s)
        logger.info("Return results: %r", results)
        return results

    def order_results(query, results: List[Document]):
        ordered_results = sorted(results, key=lambda result: Levenshtein.distance(query, result.title))
        # print("Order results", query, ordered_results, sep='\n')
        return ordered_results

    @app.get("/complete")
    def complete(q: str):
        ordered_results = get_results(q)
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
        return ordered_results

    @app.get('/')
    def index():
        return FileResponse('static/index.html')

    app.mount('/', StaticFiles(directory="static"), name="static")
    return app
