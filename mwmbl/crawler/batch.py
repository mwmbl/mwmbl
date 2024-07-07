from typing import Optional

from ninja import Schema


class Link(Schema):
    url: str
    link_type: str
    anchor_text: Optional[str] = None


class ItemContent(Schema):
    title: str
    extract: str
    link_details: Optional[list[Link]]

    # Deprecated
    links: Optional[list[str]]
    extra_links: Optional[list[str]]
    links_only: Optional[bool]

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
    message: Optional[str]


class Item(Schema):
    url: str
    resolved_url: Optional[str]
    status: Optional[int]
    timestamp: int
    content: Optional[ItemContent]
    error: Optional[ItemError]


class Batch(Schema):
    user_id: str
    items: list[Item]


class NewBatchRequest(Schema):
    user_id: str


class HashedBatch(Schema):
    user_id_hash: str
    timestamp: int
    items: list[Item]
