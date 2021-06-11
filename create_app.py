import sqlite3
from functools import lru_cache
from typing import List

import Levenshtein
from fastapi import FastAPI
from starlette.responses import RedirectResponse, FileResponse
from starlette.staticfiles import StaticFiles

from index import TinyIndex, Document


def create(tiny_index: TinyIndex):
    app = FastAPI()

    @app.get("/search")
    def search(s: str):
        if '—' in s:
            url = s.split('—')[1].strip()
        else:
            url = f'https://www.google.com/search?q={s}'
        return RedirectResponse(url)

    def order_results(query, results: List[Document]):
        ordered_results = sorted(results, key=lambda result: Levenshtein.distance(query, result.title))
        # print("Order results", query, ordered_results, sep='\n')
        return ordered_results

    @app.get("/complete")
    def complete(q: str):
        terms = [x.lower() for x in q.replace('.', ' ').split()]

        # completed = complete_term(terms[-1])
        # terms = terms[:-1] + [completed]

        pages = []
        for term in terms:
            items = tiny_index.retrieve(term)
            if items is not None:
                pages += [item for item in items if term in item.title.lower()]

        ordered_results = order_results(q, pages)
        results = [item.title.replace("\n", "") + ' — ' +
                   item.url.replace("\n", "") for item in ordered_results]
        if len(results) == 0:
            # print("No results")
            return []
        # print("Results", results)
        return [q, results]

    @app.get('/')
    def index():
        return FileResponse('static/index.html')

    app.mount('/', StaticFiles(directory="static"), name="static")
    return app
