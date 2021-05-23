import sqlite3
from functools import lru_cache

import Levenshtein
import pandas as pd

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, RedirectResponse

from index import TinyIndex, PAGE_SIZE, NUM_PAGES
from paths import INDEX_PATH

app = FastAPI()
tiny_index = TinyIndex(INDEX_PATH, NUM_PAGES, PAGE_SIZE)


@app.get("/search")
def search(s: str):
    if '—' in s:
        url = s.split('—')[1].strip()
    else:
        url = f'https://www.google.com/search?q={s}'
    return RedirectResponse(url)


@lru_cache()
def complete_term(term):
    con = sqlite3.connect(INDEX_PATH)
    query = f"""
        SELECT term
        FROM terms
        WHERE term >= ?
        ORDER BY term
        LIMIT 1
    """
    result = con.execute(query, (term,))
    completed = result.fetchone()
    # print("Completed", completed)
    if len(completed) > 0:
        return completed[0]
    return None


def order_results(query, results):
    return sorted(results, key=lambda result: Levenshtein.distance(query, result[0]))


@app.get("/complete")
def complete(q: str):
    terms = [x.lower() for x in q.split()]

    # completed = complete_term(terms[-1])
    # terms = terms[:-1] + [completed]

    pages = []
    for term in terms:
        page = tiny_index.retrieve(term)
        if page is not None:
            pages += page

    ordered_results = order_results(q, pages)
    results = [title.replace("\n", "") + ' — ' +
               url.replace("\n", "") for title, url in ordered_results]
    if len(results) == 0:
        # print("No results")
        return []
    # print("Results", results)
    return [q, results]


@app.get('/')
def index():
    return FileResponse('static/index.html')


app.mount('/', StaticFiles(directory="static"), name="static")
