from typing import Optional

from ninja import Schema, Field


class Link(Schema):
    """A hyperlink found on a crawled page."""

    url: str = Field(
        description="The absolute URL of the link target.",
        example="https://example.com/about",
    )
    link_type: str = Field(
        description=(
            "Classification of the link. `content` for links within the main body text, "
            "`nav` for navigation/header/footer links."
        ),
        example="content",
    )
    anchor_text: Optional[str] = Field(
        default=None,
        description="The visible anchor text of the link, if available.",
        example="About us",
    )


class ItemContent(Schema):
    """Extracted text content from a successfully crawled page."""

    title: str = Field(
        description="The page title, extracted from the `<title>` tag or main heading.",
        example="Python Tutorial – W3Schools",
    )
    extract: str = Field(
        description="A plain-text extract of the main body content of the page.",
        example="Python is a popular programming language. It was created by Guido van Rossum...",
    )
    link_details: Optional[list[Link]] = Field(
        default=None,
        description="Structured list of links found on the page, with type and anchor text.",
    )

    # Deprecated fields kept for backward compatibility
    links: Optional[list[str]] = Field(
        default=None,
        description="**Deprecated.** Plain list of content link URLs. Use `link_details` instead.",
    )
    extra_links: Optional[list[str]] = Field(
        default=None,
        description="**Deprecated.** Plain list of navigation link URLs. Use `link_details` instead.",
    )
    links_only: Optional[bool] = Field(
        default=None,
        description="**Deprecated.** Whether only links were extracted (no content).",
    )

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
    """An error that occurred while crawling a URL."""

    name: str = Field(
        description="The error type or class name.",
        example="ConnectionError",
    )
    message: Optional[str] = Field(
        default=None,
        description="A human-readable description of the error.",
        example="Failed to establish a new connection: [Errno -2] Name or service not known",
    )


class Item(Schema):
    """A single crawled URL and its result."""

    url: str = Field(
        description="The original URL that was crawled.",
        example="https://example.com/page",
    )
    resolved_url: Optional[str] = Field(
        default=None,
        description="The final URL after following any redirects.",
        example="https://www.example.com/page",
    )
    status: Optional[int] = Field(
        default=None,
        description="The HTTP status code returned by the server.",
        example=200,
    )
    timestamp: float = Field(
        description="Unix timestamp (seconds since epoch) when the URL was crawled.",
        example=1704672000.0,
    )
    content: Optional[ItemContent] = Field(
        default=None,
        description="Extracted page content. Present only when the crawl succeeded.",
    )
    error: Optional[ItemError] = Field(
        default=None,
        description="Error details. Present only when the crawl failed.",
    )


class Batch(Schema):
    """A batch of crawled pages submitted by a crawler client."""

    user_id: str = Field(
        description=(
            "The crawler's private user ID (a 64-character hex string). "
            "This is hashed server-side before storage — the raw ID is never persisted."
        ),
        example="a" * 64,
    )
    items: list[Item] = Field(
        description="List of crawled URLs and their results. Maximum 100 items per batch.",
    )


class NewBatchRequest(Schema):
    """Request for a new batch of URLs to crawl."""

    user_id: str = Field(
        description=(
            "The crawler's private user ID (a 64-character hex string). "
            "Used to assign URLs to this specific crawler."
        ),
        example="a" * 64,
    )


class HashedBatch(Schema):
    """A batch as stored/returned by the server, with the user ID replaced by its hash."""

    user_id_hash: str = Field(
        description="SHA3-256 hash of the crawler's user ID.",
        example="b94f6f125c79e3a5ffaa826f584c10d52ada669e6762051b826b55776d05a15",
    )
    timestamp: float = Field(
        description="Unix timestamp when the batch was received by the server.",
        example=1704672000.0,
    )
    items: list[Item] = Field(
        description="The crawled items in this batch.",
    )


class Result(Schema):
    """A single search result to be indexed."""

    url: str = Field(
        description="The URL of the page.",
        example="https://docs.python.org/3/tutorial/",
    )
    title: str = Field(
        description="The page title.",
        example="The Python Tutorial",
    )
    extract: str = Field(
        description="A short plain-text extract from the page.",
        example="Python is an easy to learn, powerful programming language...",
    )
    score: Optional[float] = Field(
        default=None,
        description="Relevance score assigned by the crawler (optional).",
        example=0.85,
    )
    term: Optional[str] = Field(
        default=None,
        description="The search term this result is associated with (optional).",
        example="python tutorial",
    )
    state: Optional[int] = Field(
        default=None,
        description="Internal state flag (optional).",
        example=1,
    )


class Results(Schema):
    """A set of search results submitted for indexing via the results API."""

    api_key: Optional[str] = Field(
        default=None,
        description=(
            "**Deprecated.** Pass your API key in the `X-API-Key` request header instead. "
            "Body-based key is still accepted for backward compatibility but will be removed in a future version."
        ),
        example=None,
    )
    results: list[Result] = Field(
        description="List of search results to index.",
    )
    crawler_version: Optional[str] = Field(
        default=None,
        description="Version string of the crawler that produced these results.",
        example="1.2.0",
    )


class PostResultsResponse(Schema):
    """Response returned after successfully submitting results for indexing."""

    status: str = Field(
        description="Always `ok` on success.",
        example="ok",
    )
    url: str = Field(
        description="Public URL of the uploaded results file in object storage.",
        example="https://storage.mwmbl.org/1/v1/2024-01-01/results/username/00001__abcd1234.json.gz",
    )


class Error(Schema):
    """A generic error response."""

    message: str = Field(
        description="Human-readable description of the error.",
        example="Invalid API key",
    )


class QueryDatasetEntry(Schema):
    """A single query autocomplete interaction recorded by the Firefox extension."""

    query: str = Field(
        description="The search query typed by the user.",
        example="wikipedia",
    )
    suggestion: str = Field(
        description="The autocomplete suggestion that was shown.",
        example="wikipedia english",
    )
    source_term: str = Field(
        description="The source term used to generate the suggestion.",
        example="wikipedia",
    )
    timestamp: int = Field(
        description="Unix timestamp in milliseconds when the interaction occurred.",
        example=1704672000000,
    )


class SearchResultEntry(Schema):
    """A single search result shown to the user by the Firefox extension."""

    title: str = Field(
        description="The page title of the search result.",
        example="Wikipedia",
    )
    url: str = Field(
        description="The URL of the search result.",
        example="https://en.wikipedia.org/",
    )
    extract: str = Field(
        description="A short extract from the page.",
        example="Wikipedia is a free online encyclopedia...",
    )
    timestamp: int = Field(
        description="Unix timestamp in milliseconds when this result was shown.",
        example=1704672000000,
    )


class SearchResultSet(Schema):
    """A complete set of search results for a single query, as recorded by the Firefox extension."""

    query: str = Field(
        description="The search query.",
        example="wikipedia english",
    )
    results: list[SearchResultEntry] = Field(
        description="The search results shown to the user.",
    )
    timestamp: int = Field(
        description="Unix timestamp in milliseconds when the search was performed.",
        example=1704672000000,
    )
    duration: int = Field(
        description="Time taken to return results, in milliseconds.",
        example=1250,
    )
    success: bool = Field(
        description="Whether the search returned results successfully.",
        example=True,
    )
    resultCount: int = Field(
        description="Number of results returned.",
        example=8,
    )
    searchIndex: int = Field(
        description="Index of the search engine used (internal identifier).",
        example=1,
    )


class DatasetRequest(Schema):
    """
    A dataset of search interactions submitted by the Firefox extension.

    Example request body::

        {
            "user_id": "aaaa...aaaa",
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
                    ],
                    "timestamp": 1704672000000,
                    "duration": 1250,
                    "success": true,
                    "resultCount": 8,
                    "searchIndex": 1
                }
            ]
        }
    """

    user_id: str = Field(
        description=(
            "The extension user's private user ID (a 64-character hex string). "
            "This is hashed server-side before storage."
        ),
        example="a" * 64,
    )
    date: str = Field(
        description="The date this dataset covers, in `YYYY-MM-DD` format.",
        example="2025-01-07",
    )
    timestamp: int = Field(
        description="Unix timestamp in milliseconds when the dataset was collected.",
        example=1704672000000,
    )
    extensionVersion: str = Field(
        description="Version of the Firefox extension that collected this data.",
        example="0.6.1",
    )
    queryDataset: list[QueryDatasetEntry] = Field(
        description="List of autocomplete interactions recorded during the session.",
    )
    searchResults: list[SearchResultSet] = Field(
        description="List of search result sets shown to the user during the session.",
    )


class HashedDataset(Schema):
    """A dataset as stored by the server, with the user ID replaced by its hash."""

    user_id_hash: str = Field(
        description="SHA3-256 hash of the extension user's user ID.",
        example="b94f6f125c79e3a5ffaa826f584c10d52ada669e6762051b826b55776d05a15",
    )
    date: str = Field(
        description="The date this dataset covers, in `YYYY-MM-DD` format.",
        example="2025-01-07",
    )
    timestamp: int = Field(
        description="Unix timestamp in milliseconds when the dataset was collected.",
        example=1704672000000,
    )
    extensionVersion: str = Field(
        description="Version of the Firefox extension that collected this data.",
        example="0.6.1",
    )
    queryDataset: list[QueryDatasetEntry] = Field(
        description="List of autocomplete interactions.",
    )
    searchResults: list[SearchResultSet] = Field(
        description="List of search result sets.",
    )
