"""
Copy an old index into a new one
"""
from collections import defaultdict
from logging import getLogger
from pathlib import Path
from time import sleep

from django.conf import settings

from mwmbl.indexer.index_batches import index_pages
from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.models import OldIndex
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


NUM_PAGES_TO_COPY = 10

logger = getLogger(__name__)


def copy_pages(old_index_path: str, new_index_path: str, start_page: int):
    logger.info(f"Copying pages from {old_index_path} to {new_index_path} starting at page {start_page}")

    # Get all old indexes:
    page_documents = defaultdict(list)
    with TinyIndex(item_factory=Document, index_path=new_index_path) as new_index:
        with TinyIndex(item_factory=Document, index_path=old_index_path) as old_index:
            # Copy each page in the old index into the new one
            for i in range(start_page, start_page + NUM_PAGES_TO_COPY):
                documents = old_index.get_page(i)
                assert all(document.term is not None for document in documents)
                for document in documents:
                    new_page = new_index.get_key_page_index(document.term)
                    page_documents[new_page].append(document)

    logger.info(f"Copying {len(page_documents)} pages to {new_index_path}")
    index_pages(new_index_path, page_documents)


def copy_all_indexes(new_index_path):
    old_indexes = OldIndex.objects.all()
    for old_index_info in old_indexes:
        copy_pages(old_index_info.index_path, new_index_path, old_index_info.start_page)


def run_continuously():
    new_index_path = Path(settings.DATA_PATH) / INDEX_NAME
    while True:
        try:
            copy_all_indexes(new_index_path)
        except Exception as e:
            logger.exception("Error copying pages")
        sleep(10)
