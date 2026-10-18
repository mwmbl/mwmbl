"""
How closely two crawlers' copies of the same URL agree.

Every submission is written to the pages of each of its terms, and a page already holding
the URL holds another crawler's copy of it. Comparing the two at that point costs nothing,
and a crawler whose copies disagree with many different crawlers is one whose submissions
are worth a closer look.
"""

from typing import Iterable, NamedTuple, Optional

from mwmbl.tinysearchengine.indexer import CURATED_STATES, Document


class Agreement(NamedTuple):
    url: str
    existing_user_id: int
    """The crawler whose text the stored copy holds: the last of its user_ids, which
    combine_documents keeps for the kept copy's author."""
    title_similarity: float
    extract_similarity: float
    last_crawled_delta: Optional[int]
    """Seconds from the stored copy's crawl to the incoming one's: pages change, so a large
    gap explains a low similarity that a small one does not. The stored crawl time is the
    latest of the copies merged into it, not necessarily that of its text."""


def find_agreements(incoming: Iterable[Document], existing: Iterable[Document]) -> set[Agreement]:
    """Compare each incoming document against another crawler's stored copy of its URL."""
    # A page keeps one document per URL, so there is at most one stored copy to compare. A
    # curated copy has no author of its own but always wins its page, so it takes on the
    # user_ids of the copies it beat, and its last id names one of them.
    authors_by_url = {
        document.url: (document, document.user_ids[-1])
        for document in existing
        if document.user_ids and document.state not in CURATED_STATES
    }
    agreements = set()
    for document in incoming:
        if document.url not in authors_by_url or document.user_ids is None:
            continue
        stored, existing_user_id = authors_by_url[document.url]
        # The stored text may be this crawler's own, from an earlier submission.
        if existing_user_id in document.user_ids:
            continue
        both_crawled = document.last_crawled is not None and stored.last_crawled is not None
        last_crawled_delta = document.last_crawled - stored.last_crawled if both_crawled else None
        agreements.add(
            Agreement(
                url=document.url,
                existing_user_id=existing_user_id,
                title_similarity=trigram_similarity(document.title, stored.title),
                extract_similarity=trigram_similarity(document.extract, stored.extract),
                last_crawled_delta=last_crawled_delta,
            )
        )
    return agreements


def trigram_similarity(first: str, second: str) -> float:
    """Jaccard similarity of the two strings' character trigrams."""
    first_trigrams = _trigrams(first)
    second_trigrams = _trigrams(second)
    return len(first_trigrams & second_trigrams) / len(first_trigrams | second_trigrams)


def _trigrams(text: str) -> set[str]:
    normalised = " ".join(text.lower().split())
    # A string shorter than a trigram is its own only gram, so "" matches "" and "a" does not match "b".
    return {normalised[i : i + 3] for i in range(max(len(normalised) - 2, 1))}
