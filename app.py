from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, RedirectResponse

app = FastAPI()


@app.get("/search")
def search(s: str):
    return RedirectResponse(f'https://www.google.com/search?q={s}')


@app.get("/complete")
def complete(q: str):
    all_titles = ['some', 'nice results', 'here']
    return [q, all_titles]


@app.get('/')
def index():
    return FileResponse('static/index.html')


app.mount('/', StaticFiles(directory="static"), name="static")
