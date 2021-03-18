import sqlite3
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


def complete_term(term):
    con = sqlite3.connect(INDEX_PATH)
    query = f"""
        SELECT term, count(*)
        FROM terms
        WHERE term LIKE (? || '%')
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
        SELECT title, url, count(*)
        FROM terms INNER JOIN pages
        ON terms.page_id = pages.id
        WHERE term IN ({in_part})
        GROUP BY title, url
        ORDER BY 3 DESC
    """

    data = pd.read_sql(query, con, params=terms)
    results = data.apply(lambda row: f'{row.title} — {row.url}', axis=1)
    print("Results", results)
    if len(results) == 0:
        return []
    return [q, results.to_list()[:5]]


@app.get('/')
def index():
    return FileResponse('static/index.html')


app.mount('/', StaticFiles(directory="static"), name="static")
