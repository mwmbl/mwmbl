"""
How closely two crawlers' copies of the same URL agree.

Every submission is written to the pages of each of its terms, and a page already holding
the URL holds another crawler's copy of it. Comparing the two at that point costs nothing,
and a crawler whose copies disagree with many different crawlers is one whose submissions
are worth a closer look.
"""

from collections import defaultdict
from typing import Iterable, NamedTuple, Optional

from mwmbl.tinysearchengine.indexer import CURATED_STATES, Document


class StoredCopy(NamedTuple):
    """Another crawler's copy of a URL, as a page holds it: what pages are counted on to find
    the copy most of a URL's pages hold. Its crawl time is left out, since that is the latest
    of the copies merged on each page and so can differ between pages holding the same copy."""

    url: str
    author_id: int
    """The last of its user_ids, which combine_documents keeps for the kept copy's author."""
    title: str
    extract: str


class Agreement(NamedTuple):
    url: str
    existing_user_id: int
    title_similarity: float
    extract_similarity: float
    extract_empty: bool
    """Whether the incoming copy's extract is empty. A copy with an empty extract scores zero
    against any other, and a quarter of the agreements replayed from a real crawl had one, so
    these need telling apart from copies that disagree."""
    existing_extract_empty: bool
    last_crawled_delta: Optional[int]
    """Seconds from the stored copy's crawl to the incoming one's: pages change, so a large
    gap explains a low similarity that a small one does not. The stored crawl time is the
    latest of the copies merged into it, not necessarily that of its text."""


def find_stored_copies(
    incoming: Iterable[Document], existing: Iterable[Document]
) -> list[tuple[StoredCopy, Optional[int]]]:
    """Other crawlers' stored copies of the incoming documents' URLs, with their crawl times."""
    submitters_by_url = defaultdict(set)
    for document in incoming:
        submitters_by_url[document.url] |= set(document.user_ids or ())
    stored_copies = []
    # A page keeps one document per URL, so there is at most one stored copy of each.
    for document in existing:
        # A curated copy or a copy of Staan's has no author of its own, yet takes on the
        # user_ids of the copies it beats, so its last id names one of them.
        attributed = document.user_ids and document.state not in CURATED_STATES and document.source is None
        if not attributed or not submitters_by_url[document.url]:
            continue
        author_id = document.user_ids[-1]
        # The stored text may be this crawler's own, from an earlier submission.
        if author_id in submitters_by_url[document.url]:
            continue
        stored_copy = StoredCopy(document.url, author_id, document.title, document.extract)
        stored_copies.append((stored_copy, document.last_crawled))
    return stored_copies


def compare(document: Document, stored_copy: StoredCopy, stored_last_crawled: Optional[int]) -> Agreement:
    both_crawled = document.last_crawled is not None and stored_last_crawled is not None
    return Agreement(
        url=document.url,
        existing_user_id=stored_copy.author_id,
        title_similarity=trigram_similarity(document.title, stored_copy.title),
        extract_similarity=trigram_similarity(document.extract, stored_copy.extract),
        extract_empty=_is_empty(document.extract),
        existing_extract_empty=_is_empty(stored_copy.extract),
        last_crawled_delta=document.last_crawled - stored_last_crawled if both_crawled else None,
    )


def trigram_similarity(first: str, second: str) -> float:
    """Jaccard similarity of the two strings' character trigrams."""
    first_trigrams = _trigrams(first)
    second_trigrams = _trigrams(second)
    return len(first_trigrams & second_trigrams) / len(first_trigrams | second_trigrams)


def _is_empty(text: str) -> bool:
    return not text.strip()


def _trigrams(text: str) -> set[str]:
    normalised = " ".join(text.lower().split())
    # A string shorter than a trigram is its own only gram, so "" matches "" and "a" does not match "b".
    return {normalised[i : i + 3] for i in range(max(len(normalised) - 2, 1))}
