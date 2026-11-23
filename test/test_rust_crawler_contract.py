"""The results API as the Rust crawler speaks it.

`rust/crawler/fixtures/` holds a request exactly as the crawler serialises it and a response
it must be able to read, both pinned by the crawler's own tests (`src/batch.rs`). These tests
check the same files against the server's side, so a field renamed or retyped on either side
fails one of the two suites.
"""

import dataclasses
import json
from pathlib import Path

from mwmbl.crawler.batch import PostResultsResponse, Result, Results
from mwmbl.tinysearchengine.indexer import Document, DocumentState

FIXTURES = Path(__file__).parent.parent / "rust" / "crawler" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_the_crawlers_request_is_valid_and_uses_only_fields_the_server_reads():
    request = load_fixture("results_request.json")

    results = Results.model_validate(request)

    assert set(request) <= set(Results.model_fields)
    assert set(request["results"][0]) <= set(Result.model_fields)
    assert results.crawler_version == "2.0.0"
    assert results.results[0].state == DocumentState.FROM_USER.value


def test_the_crawler_reads_new_items_in_the_shape_the_server_sends_them():
    response = load_fixture("results_response.json")
    # What post_results puts in new_items for each document.
    sent_item = dataclasses.asdict(Document(title="", url="", extract=""))

    PostResultsResponse.model_validate(response)

    assert set(response["new_items"][0]) == set(sent_item)
    assert response["new_items"][0]["state"] == DocumentState.ORGANIC_APPROVED.value
