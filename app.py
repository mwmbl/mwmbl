import uvicorn

import create_app

from index import TinyIndex, PAGE_SIZE, NUM_PAGES, Document
from paths import INDEX_PATH

tiny_index = TinyIndex(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE)
app = create_app.create(tiny_index)


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, log_level="info")
