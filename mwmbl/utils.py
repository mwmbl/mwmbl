import re
from dataclasses import dataclass
from typing import Sequence

from django.core.exceptions import ValidationError

from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

DOMAIN_REGEX = re.compile(r".*://([^/]*)")


def batch(items: Sequence, batch_size):
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


@dataclass
class ParsedUrl:
    scheme: str
    netloc: str
    query_string: str
    fragment: str


# See https://stackoverflow.com/a/2627127/660902
URL_REGEX = re.compile("^(([^:/?#]+):)?(//([^/?#]*)|///)?([^?#]*)(\\?[^#]*)?(#.*)?")


def parse_url(url: str) -> ParsedUrl:
    """
    Custom URL parsing function using regex because urlparse is too slow.
    """
    results = URL_REGEX.match(url)
    if results is None:
        raise ValueError(f"Unable to parse URL {url}")
    return ParsedUrl(results.group(2), results.group(4), results.group(6), results.group(7))


VALID_DOMAIN_REGEX = re.compile(r"^[\w-]{1,63}(\.[\w-]{1,63})+$")


def validate_domain(domain_or_url: str):
    if VALID_DOMAIN_REGEX.fullmatch(domain_or_url) is None:
        # See if we can extract a domain from the URL
        try:
            domain = parse_url(domain_or_url).netloc
        except ValueError:
            raise ValidationError("Invalid domain: %(domain)s", params={"domain": domain_or_url})
        if domain is None or VALID_DOMAIN_REGEX.fullmatch(domain) is None:
            raise ValidationError("Invalid domain: %(domain)s", params={"domain": domain_or_url})

