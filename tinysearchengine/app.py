import logging

import uvicorn

from tinysearchengine import create_app

from tinysearchengine.indexer import TinyIndex, NUM_PAGES, PAGE_SIZE, Document
from paths import INDEX_PATH

tiny_index = TinyIndex(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE)
app = create_app.create(tiny_index)

logging.basicConfig()


if __name__ == "__main__":
    uvicorn.run("tinysearchengine.app:app", host="127.0.0.1", port=8080, log_level="info", reload=True)
