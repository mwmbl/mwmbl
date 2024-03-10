"""
Copy an old index into a new one
"""
from collections import defaultdict
from logging import getLogger

from mwmbl.indexer.index_batches import index_pages, get_url_score
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.utils import add_term_infos

logger = getLogger(__name__)


def copy_pages(old_index_path: str, new_index_path: str, start_page: int, num_pages_to_copy):
    logger.info(f"Copying pages from {old_index_path} to {new_index_path} starting at page {start_page}")

    # Get all old indexes:
    page_documents = defaultdict(list)
    with (TinyIndex(item_factory=Document, index_path=new_index_path) as new_index):
        with TinyIndex(item_factory=Document, index_path=old_index_path) as old_index:
            logger.info(f"Old index has {old_index.num_pages} pages")

            # Copy each page in the old index into the new one
            for page_index in range(start_page, start_page + num_pages_to_copy):
                if page_index >= old_index.num_pages:
                    break

                documents = old_index.get_page(page_index)
                documents_with_terms = add_term_infos(documents, old_index, page_index)
                documents_with_scores = [Document(
                    document.title,
                    document.url,
                    document.extract,
                    get_url_score(document.url),
                    document.term,
                    state=document.state
                ) for document in documents_with_terms]
                for document in documents_with_scores:
                    new_page = new_index.get_key_page_index(document.term)
                    page_documents[new_page].append(document)

    logger.info(f"Copying {len(page_documents)} pages to {new_index_path}")
    index_pages(new_index_path, page_documents)

    return page_index
