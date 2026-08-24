import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Sequence, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from redis import Redis
from requests_cache import CachedSession, RedisCache
from requests.adapters import Retry, HTTPAdapter

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

# Everything from the first of these characters onwards is a path, query string or fragment.
DOMAIN_END_REGEX = re.compile(r"[/?#]")


def normalize_domain(domain_or_url: str) -> str:
    """
    Extract the domain from a URL. A bare domain is returned unchanged, and any path, query string
    or fragment is stripped. The domain is lowercased so that a domain has a single representation
    however it was typed: domains are compared as strings elsewhere, e.g. against the curated
    domains when deciding how many URLs to queue for a domain.
    """
    netloc = parse_url(domain_or_url).netloc
    if not netloc:
        netloc = DOMAIN_END_REGEX.split(domain_or_url, maxsplit=1)[0]
    return netloc.lower()


def validate_domain(domain_or_url: str):
    domain = normalize_domain(domain_or_url)
    if VALID_DOMAIN_REGEX.fullmatch(domain) is None:
        raise ValidationError("Invalid domain: %(domain)s", params={"domain": domain_or_url})


def request_cache(expire_after: Optional[timedelta] = None) -> CachedSession:
    return CachedSession(expire_after=expire_after, backend="filesystem", cache_name=settings.REQUEST_CACHE_PATH)


def float_or_none(s: str) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        return None
