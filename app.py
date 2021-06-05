import create_app

from index import TinyIndex, PAGE_SIZE, NUM_PAGES
from paths import INDEX_PATH

tiny_index = TinyIndex(INDEX_PATH, NUM_PAGES, PAGE_SIZE)
app = create_app.create(tiny_index)
