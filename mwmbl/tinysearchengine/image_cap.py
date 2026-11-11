"""At most a few Wikimedia Commons images in one result list (#610).

Commons file pages are indexed as documents like any other page, so a query whose terms match many
file names can fill the list with pictures of one thing. The holistic evaluation (#626) found nine
station photos for one query losing badly, and a cap of 2 per list beating both no cap and a cap of 1.

ImageCapRanker wraps the outermost ranker, so the cap applies to the final order (after the LTR and
MMR) and before any caller takes its top ten: images past the cap are dropped, and the web results
below them move up.
"""

from typing import Protocol

from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import Retrieval


class SearchRanker(Protocol):
    """What the served rankers have in common: a Ranker, or a decorator such as MMRRanker or JevRanker around one."""

    def search(
        self, s: str, additional_results: list[Document], use_external_search: bool = True
    ) -> list[Document]: ...

    def retrieve(self, q: str) -> Retrieval: ...

    def search_retrieved(self, retrieval: Retrieval, additional_results: list[Document]) -> list[Document]: ...

    def complete(self, q: str): ...

    def get_raw_results(self, query: str): ...


def cap_images(results: list[Document], cap: int) -> list[Document]:
    """The results without every Commons file page after the first `cap`, in their order."""
    kept = []
    images_kept = 0
    for result in results:
        # Any URL for a Commons file page counts, approved or not: a crawled one looks the same to the ranker.
        is_image = commons_file_key(result.url) is not None
        if is_image and images_kept == cap:
            continue
        kept.append(result)
        images_kept += is_image
    return kept


class ImageCapRanker:
    """Decorator that keeps at most `cap` Commons images in a ranker's search results.

    Delegates retrieval, completion and raw results unchanged.
    """

    def __init__(self, ranker: SearchRanker, cap: int):
        self.ranker = ranker
        self.cap = cap

    def search(self, s: str, additional_results: list[Document], use_external_search: bool = True) -> list[Document]:
        return cap_images(self.ranker.search(s, additional_results, use_external_search), self.cap)

    def retrieve(self, q: str) -> Retrieval:
        return self.ranker.retrieve(q)

    def search_retrieved(self, retrieval: Retrieval, additional_results: list[Document]) -> list[Document]:
        return cap_images(self.ranker.search_retrieved(retrieval, additional_results), self.cap)

    def complete(self, q: str):
        return self.ranker.complete(q)

    def get_raw_results(self, query: str):
        return self.ranker.get_raw_results(query)
