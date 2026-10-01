"""JevRanker: Combined Search's final ordering, Jev composite + Staan rank."""

from unittest.mock import MagicMock

import pytest
import requests

import mwmbl.tinysearchengine.jev_rank as jev_rank
from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.jev_rank import (
    NUM_LTR_CANDIDATES,
    NUM_RESULTS,
    QUALITY_QUESTION,
    RELEVANCE_QUESTION,
    JevRanker,
    candidate_pool,
    composite_order,
    jev_request,
)
from mwmbl.tinysearchengine.mmr_rank import mmr_rerank
from mwmbl.tinysearchengine.rank import Ranker
from mwmbl.tinysearchengine.staan import staan_score

QUERY = "bankside hotel"


def staan_result(url: str, rank: int) -> Document:
    return Document(f"Staan {rank}", url, "", staan_score(rank), source=DocumentSource.STAAN)


def index_result(url: str) -> Document:
    return Document(f"Index {url}", url, "", 1.0)


class FakeRanker(Ranker):
    """Returns a fixed LTR ordering, whatever it is asked."""

    def __init__(self, ranked: list[Document]):
        super().__init__(None, None)
        self.ranked = ranked

    def search(self, s, additional_results, use_external_search=True):
        return self.ranked


def jev_answers(scores: list[tuple[float, float]]) -> dict:
    answers = {}
    for i, (relevance, quality) in enumerate(scores):
        answers[f"r{i}"] = {"score": relevance}
        answers[f"q{i}"] = {"score": quality}
    return {"answers": answers}


@pytest.fixture(autouse=True)
def no_blacklist(monkeypatch):
    monkeypatch.setattr(jev_rank, "find_blacklisted_urls", lambda documents: set())


@pytest.fixture
def jev(monkeypatch, settings):
    """Stub the Jev endpoint, recording each request body."""
    settings.JEV_API_KEY = "test-key"
    requests_made = []

    def configure(scores_for):
        def fake_post(url, json, headers, timeout):
            requests_made.append(json)
            num_candidates = len(json["questions"]) // 2
            response = MagicMock()
            response.json.return_value = jev_answers(scores_for(num_candidates))
            return response

        monkeypatch.setattr(jev_rank.session, "post", fake_post)
        return requests_made

    return configure


def test_the_request_asks_relevance_and_quality_for_every_candidate(settings):
    documents = [staan_result("https://a.com/", 0), index_result("https://b.com/")]

    body = jev_request(QUERY, documents)

    assert body["model"] == settings.JEV_MODEL
    assert body["state"] == {"query": QUERY, "searcher": "a web searcher in the United Kingdom"}
    assert set(body["questions"]) == {"r0", "q0", "r1", "q1"}
    assert body["questions"]["r1"]["instructions"]["question"] == RELEVANCE_QUESTION
    assert body["questions"]["q1"]["instructions"]["question"] == QUALITY_QUESTION
    assert body["questions"]["r1"]["instructions"]["result"] == {
        "title": "Index https://b.com/",
        "url": "https://b.com/",
        "snippet": "",
    }
    assert len(body["questions"]["r0"]["criteria"]) == 4
    assert len(body["questions"]["q0"]["criteria"]) == 3


def test_composite_weights_relevance_quality_staan_position_and_the_index_penalty():
    staan_top = staan_result("https://staan-top.com/", 0)
    staan_fifth = staan_result("https://staan-fifth.com/", 5)
    index_page = index_result("https://index.com/")
    pool = [staan_top, staan_fifth, index_page]
    # staan_top:   2.0 + 0.5 x 1 - 0.1 x 0          = 2.5
    # staan_fifth: 2.6 + 0.5 x 2 - 0.1 x 5          = 3.1
    # index_page:  2.8 + 0.5 x 2 - 0.1 x 10 - 0.5   = 2.3
    scores = jev_rank.np.array([[2.0, 1.0], [2.6, 2.0], [2.8, 2.0]])

    assert composite_order(pool, scores) == [staan_fifth, staan_top, index_page]


def test_ties_keep_staans_order():
    pool = [staan_result("https://a.com/", 0), staan_result("https://b.com/", 5)]
    # b's extra relevance exactly offsets its lower Staan position.
    scores = jev_rank.np.array([[2.0, 2.0], [2.5, 2.0]])

    assert composite_order(pool, scores) == pool


def test_the_pool_is_staan_then_the_ltrs_top_results_each_url_once():
    staan = [staan_result("https://a.com/", 0), staan_result("https://b.com/", 1)]
    ranked = [index_result("https://b.com/")] + [index_result(f"https://{i}.com/") for i in range(40)]

    pool = candidate_pool(staan, ranked)

    assert [document.url for document in pool[:2]] == ["https://a.com/", "https://b.com/"]
    assert len(pool) == 1 + NUM_LTR_CANDIDATES
    assert pool[-1].url == f"https://{NUM_LTR_CANDIDATES - 2}.com/"


def test_blacklisted_staan_results_stay_out_of_the_pool(monkeypatch):
    monkeypatch.setattr(jev_rank, "find_blacklisted_urls", lambda documents: {"https://spam.com/"})
    staan = [staan_result("https://spam.com/", 0), staan_result("https://a.com/", 1)]

    assert [document.url for document in candidate_pool(staan, [])] == ["https://a.com/"]


def test_search_serves_the_composite_top_ten(jev):
    staan = [staan_result(f"https://staan{i}.com/", i) for i in range(10)]
    ranked = [index_result(f"https://index{i}.com/") for i in range(30)]
    # Every candidate equally good: Staan's position and the index penalty decide.
    requests_made = jev(lambda n: [(2.0, 2.0)] * n)

    results = JevRanker(FakeRanker(ranked)).search(QUERY, staan, False)

    assert len(requests_made) == 1
    assert len(requests_made[0]["questions"]) == 2 * 40
    assert results == staan[:NUM_RESULTS]


def test_a_strong_index_result_outranks_a_weak_staan_result(jev):
    staan = [staan_result("https://staan.com/", 0)]
    ranked = [index_result("https://index.com/")]
    jev(lambda n: [(1.0, 1.0), (3.0, 2.0)])

    results = JevRanker(FakeRanker(ranked)).search(QUERY, staan, False)

    assert [document.url for document in results] == ["https://index.com/", "https://staan.com/"]


@pytest.mark.parametrize("error", [requests.Timeout(), requests.HTTPError(), KeyError("answers")])
def test_a_failed_jev_call_falls_back_to_the_ltr_and_mmr_ordering(monkeypatch, settings, error):
    settings.JEV_API_KEY = "test-key"

    def failing_post(*args, **kwargs):
        raise error

    monkeypatch.setattr(jev_rank.session, "post", failing_post)
    ranked = [index_result(f"https://example.com/{i}") for i in range(3)] + [index_result("https://other.com/")]

    results = JevRanker(FakeRanker(ranked)).search(QUERY, [], False)

    assert results == mmr_rerank(ranked)


def test_without_a_key_jev_is_not_called(monkeypatch, settings):
    settings.JEV_API_KEY = ""
    post = MagicMock()
    monkeypatch.setattr(jev_rank.session, "post", post)
    ranked = [index_result("https://a.com/")]

    results = JevRanker(FakeRanker(ranked)).search(QUERY, [], False)

    post.assert_not_called()
    assert results == ranked


def test_an_empty_pool_is_not_sent_to_jev(jev):
    requests_made = jev(lambda n: [])

    assert JevRanker(FakeRanker([])).search(QUERY, [], False) == []
    assert requests_made == []
