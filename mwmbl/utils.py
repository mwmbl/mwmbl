import re

from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

DOMAIN_REGEX = re.compile(r".*://([^/]*)")


def batch(items: list, batch_size):
    """
    Adapted from https://stackoverflow.com/a/8290508
    """
    length = len(items)
    for ndx in range(0, length, batch_size):
        yield items[ndx:min(ndx + batch_size, length)]


def get_domain(url):
    results = DOMAIN_REGEX.match(url)
    if results is None or len(results.groups()) == 0:
        raise ValueError(f"Unable to parse domain from URL {url}")
    return results.group(1)


def add_term_info(document: Document, index: TinyIndex, page_index: int):
    tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
    for token in tokenized.tokens:
        token_page_index = index.get_key_page_index(token)
        if token_page_index == page_index:
            return Document(document.title, document.url, document.extract, document.score, token)
    raise ValueError("Could not find token in page index")


def add_term_infos(documents: list[Document], index: TinyIndex, page_index: int):
    for document in documents:
        if document.term is not None:
            yield document
            continue
        try:
            yield add_term_info(document, index, page_index)
        except ValueError:
            continue
