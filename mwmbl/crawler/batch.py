from typing import Optional

from pydantic import BaseModel


class ItemContent(BaseModel):
    title: str
    extract: str
    links: list[str]


class ItemError(BaseModel):
    name: str
    message: Optional[str]


class Item(BaseModel):
    url: str
    status: Optional[int]
    timestamp: int
    content: Optional[ItemContent]
    error: Optional[ItemError]


class Batch(BaseModel):
    user_id: str
    items: list[Item]


class NewBatchRequest(BaseModel):
    user_id: str


class HashedBatch(BaseModel):
    user_id_hash: str
    timestamp: int
    items: list[Item]
