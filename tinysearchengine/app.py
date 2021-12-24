import logging
import sys

import uvicorn

from tinysearchengine import create_app
from tinysearchengine.indexer import TinyIndex, NUM_PAGES, PAGE_SIZE, Document

logging.basicConfig()


index_path = sys.argv[1]
tiny_index = TinyIndex(Document, index_path, NUM_PAGES, PAGE_SIZE)
app = create_app.create(tiny_index)

if __name__ == "__main__":
    uvicorn.run("tinysearchengine.app:app", host="0.0.0.0", port=8080, log_level="info")
