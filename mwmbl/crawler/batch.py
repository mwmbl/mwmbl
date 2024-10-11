from typing import Optional

from ninja import Schema


class Link(Schema):
    url: str
    link_type: str
    anchor_text: Optional[str] = None


class ItemContent(Schema):
    title: str
    extract: str
    link_details: Optional[list[Link]] = None

    # Deprecated
    links: Optional[list[str]] = None
    extra_links: Optional[list[str]] = None
    links_only: Optional[bool] = None

    @property
    def all_links(self) -> list[Link]:
        links = []
        if self.link_details:
            links += self.link_details
        if self.links:
            links += [Link(url=link, link_type="content") for link in self.links]
        if self.extra_links:
            links += [Link(url=link, link_type="nav") for link in self.extra_links]
        return links


class ItemError(Schema):
    name: str
    message: Optional[str] = None


class Item(Schema):
    url: str
    resolved_url: Optional[str] = None
    status: Optional[int] = None
    timestamp: int
    content: Optional[ItemContent] = None
    error: Optional[ItemError] = None


class Batch(Schema):
    user_id: str
    items: list[Item]


class NewBatchRequest(Schema):
    user_id: str


class HashedBatch(Schema):
    user_id_hash: str
    timestamp: int
    items: list[Item]


class Result(Schema):
    url: str
    title: str
    extract: str
    score: Optional[float] = None
    term: Optional[str] = None
    state: Optional[int] = None


class Results(Schema):
    api_key: str
    results: list[Result]


class PostResultsResponse(Schema):
    status: str
    url: str


class Error(Schema):
    message: str
