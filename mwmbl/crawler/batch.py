from typing import Optional

from ninja import Schema


class Link(Schema):
    url: str
    anchor_text: str
    link_type: str


class ItemContent(Schema):
    title: str
    extract: str
    link_details: Optional[list[Link]]

    # Deprecated
    links: Optional[list[str]]
    extra_links: Optional[list[str]]
    links_only: Optional[bool]


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
