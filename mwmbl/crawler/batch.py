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
    timestamp: float
    content: Optional[ItemContent] = None
    error: Optional[ItemError] = None


class Batch(Schema):
    user_id: str
    items: list[Item]


class NewBatchRequest(Schema):
    user_id: str


class HashedBatch(Schema):
    user_id_hash: str
    timestamp: float
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
    crawler_version: Optional[str] = None


class PostResultsResponse(Schema):
    status: str
    url: str


class Error(Schema):
    message: str


class QueryDatasetEntry(Schema):
    query: str
    suggestion: str
    source_term: str
    timestamp: int


class SearchResultEntry(Schema):
    title: str
    url: str
    extract: str
    timestamp: int


class SearchResultSet(Schema):
    query: str
    results: list[SearchResultEntry]
    timestamp: int
    duration: int
    success: bool
    resultCount: int
    searchIndex: int


class DatasetRequest(Schema):
    user_id: str
    date: str
    timestamp: int
    extensionVersion: str
    queryDataset: list[QueryDatasetEntry]
    searchResults: list[SearchResultSet]


"""
Data format for requests to dataset backend

{
    "user_id": "user123456789012345678901234567890123456789012345678901234567890",
    "date": "2025-01-07",
    "timestamp": 1704672000000,
    "extensionVersion": "0.6.1",
    "queryDataset": [
        {
            "query": "wikipedia",
            "suggestion": "wikipedia english",
            "source_term": "wikipedia",
            "timestamp": 1704672000000
        }
        // ... more entries
    ],
    "searchResults": [
        {
            "query": "wikipedia english",
            "results": [
                {
                    "title": "Wikipedia",
                    "url": "https://en.wikipedia.org/",
                    "extract": "Wikipedia is a free online encyclopedia...",
                    "timestamp": 1704672000000
                }
                // ... more results
            ],
            "timestamp": 1704672000000,
            "duration": 1250,
            "success": true,
            "resultCount": 8,
            "searchIndex": 1
        }
        // ... more searches
    ]
}
"""
