from datetime import datetime
from typing import TypeVar, Generic

from ninja import Schema


class Result(Schema):
    url: str
    title: str
    extract: str
    curated: bool


class CurateBegin(Schema):
    pass


class CurateMove(Schema):
    old_index: int
    new_index: int


class CurateDelete(Schema):
    delete_index: int


class CurateAdd(Schema):
    insert_index: int
    url: str


class CurateValidate(Schema):
    validate_index: int
    is_validated: bool


T = TypeVar('T', CurateBegin, CurateAdd, CurateDelete, CurateMove, CurateValidate)


class Curation(Schema, Generic[T]):
    timestamp: int
    url: str
    results: list[Result]
    curation: T

