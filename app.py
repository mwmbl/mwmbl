import sqlite3
from functools import lru_cache

import pandas as pd

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, RedirectResponse

from paths import INDEX_PATH

app = FastAPI()


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
        WHERE term > ?
        ORDER BY term
        LIMIT 1
    """
    result = con.execute(query, (term,))
    completed = result.fetchone()
    print("Completed", completed)
    if len(completed) > 0:
        return completed[0]
    return None


@app.get("/complete")
def complete(q: str):
    terms = [x.lower() for x in q.split()]

    completed = complete_term(terms[-1])
    terms = terms[:-1] + [completed]

    con = sqlite3.connect(INDEX_PATH)
    in_part = ','.join('?'*len(terms))
    query = f"""
        SELECT title, url, count(*), length(title)
        FROM terms INNER JOIN pages
        ON terms.page_id = pages.id
        WHERE term IN ({in_part})
        GROUP BY title, url
        ORDER BY 3 DESC, 4
        LIMIT 20
    """

    data = pd.read_sql(query, con, params=terms)
    results = data.apply(lambda row: row.title.replace("\n", "") + ' — ' +
                                     row.url.replace("\n", ""), axis=1)
    if len(results) == 0:
        return []
    results_list = results.to_list()[:5]
    results_list = [q, results_list]
    print("Results", results_list)
    return results_list


@app.get('/')
def index():
    return FileResponse('static/index.html')


app.mount('/', StaticFiles(directory="static"), name="static")
