"""Seed Search's image flow, with Commons and the judge stubbed (no network)."""

import asyncio
import dataclasses
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from django.core.cache import cache
from test_image_search import make_image

from mwmbl.imagesearch import commons, seed_images, verdict_cache
from mwmbl.imagesearch.seed_images import ImageOutcome
from mwmbl.imagesearch.types import ImageSourceUnavailable
from mwmbl.tinysearchengine import jev_rank
from mwmbl.tinysearchengine.indexer import Document

STAAN = [Document("Red panda - Wikipedia", "https://en.wikipedia.org/wiki/Red_panda", "A mammal")]
NAMES = ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "f.jpg"]
CANDIDATES = [make_image(name, rank) for rank, name in enumerate(NAMES)]
RELEVANCE = {"a.jpg": 1.6, "b.jpg": 2.9, "c.jpg": 1.4, "d.jpg": 2.9, "e.jpg": 2.0, "f.jpg": 2.5}
NSFW = {"e.jpg": 1.0}


@pytest.fixture(autouse=True)
def setup(settings):
    settings.JEV_API_KEY = "test-key"
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def found(monkeypatch):
    """Stub the Commons search: it finds `found.images`, and records each query it was asked."""
    state = SimpleNamespace(images=CANDIDATES, queries=[])

    def fake_search(client, query, limit):
        state.queries.append(query)
        return state.images

    monkeypatch.setattr(commons, "search_commons", fake_search)
    return state


@pytest.fixture
def answers(monkeypatch):
    """Stub the judge: NSFW and relevance scores by file name, a fixed need, and a delay for the request with need,
    which writes nothing to the cache, so that a thread it leaves running cannot touch a later test."""
    nsfw = {name: NSFW.get(name, 0.0) for name in NAMES}
    state = SimpleNamespace(need=2.0, relevance=RELEVANCE, nsfw=nsfw, delay=0.0, requests=[])

    def fake_post(url, json, headers, timeout):
        time.sleep(state.delay if "need" in json["questions"] else 0)
        state.requests.append(json)
        scores = {}
        for key, question in json["questions"].items():
            text = question["instructions"].get("image", "")
            name = next((name for name in NAMES if f"File name: {name}" in text), None)
            scores[key] = state.need if key == "need" else (state.nsfw if key[0] == "n" else state.relevance)[name]
        response = MagicMock()
        response.json.return_value = {"answers": {key: {"score": score} for key, score in scores.items()}}
        return response

    monkeypatch.setattr(jev_rank.session, "post", fake_post)
    return state


async def search(query="red panda", staan=STAAN, ranking_seconds=0.0):
    image_search = seed_images.start(query)
    web_ranking = asyncio.ensure_future(asyncio.sleep(ranking_seconds))
    return await seed_images.finish(image_search, staan, web_ranking)


def titles(selection):
    return [ranked.image.title for ranked in selection.images]


async def test_every_passing_image_is_returned_most_relevant_first(found, answers):
    selection = await search()

    assert selection.outcome == ImageOutcome.SHOWN
    # e.jpg is not below the NSFW threshold and c.jpg is under the relevance minimum; ties keep Commons' order.
    assert titles(selection) == ["b.jpg", "d.jpg", "f.jpg", "a.jpg"]
    assert [ranked.relevance for ranked in selection.images] == [2.9, 2.9, 2.5, 1.6]


async def test_relevance_is_asked_about_every_candidate_without_waiting_for_nsfw(found, answers):
    await search()

    image_request = next(request for request in answers.requests if "need" in request["questions"])
    assert len(image_request["questions"]) == len(NAMES) + 1
    assert image_request["state"]["query"] == "red panda"
    nsfw_request = next(request for request in answers.requests if "need" not in request["questions"])
    assert "red panda" not in str(nsfw_request["state"])


async def test_a_query_below_the_need_gate_has_no_images(found, answers):
    answers.need = 1.6
    selection = await search()
    assert (selection.outcome, selection.images) == (ImageOutcome.NOT_NEEDED, [])


async def test_no_relevant_image_is_none_relevant(found, answers):
    answers.relevance = dict.fromkeys(NAMES, 1.0)
    assert (await search()).outcome == ImageOutcome.NONE_RELEVANT


async def test_no_candidates_is_none_relevant_without_asking_the_judge(found, answers):
    found.images = []
    assert (await search()).outcome == ImageOutcome.NONE_RELEVANT
    assert answers.requests == []


async def test_a_refused_query_asks_nobody(found, answers):
    selection = await search("nude beach")
    assert selection.outcome == ImageOutcome.QUERY_REFUSED
    assert (found.queries, answers.requests) == ([], [])


async def test_without_a_judge_key_nothing_is_asked(found, answers, settings):
    settings.JEV_API_KEY = ""
    assert (await search()).outcome == ImageOutcome.UNAVAILABLE
    assert (found.queries, answers.requests) == ([], [])


async def test_without_staan_results_the_gate_is_not_asked(found, answers):
    assert (await search(staan=[])).outcome == ImageOutcome.UNAVAILABLE
    assert not any("need" in request["questions"] for request in answers.requests)


def fail_commons(found, answers, monkeypatch):
    monkeypatch.setattr(commons, "search_commons", MagicMock(side_effect=ImageSourceUnavailable("429")))


def fail_judge(found, answers, monkeypatch):
    monkeypatch.setattr(jev_rank.session, "post", MagicMock(side_effect=requests.Timeout()))


def off_scale_need(found, answers, monkeypatch):
    answers.need = 3.5


def off_scale_relevance(found, answers, monkeypatch):
    answers.relevance = RELEVANCE | {"a.jpg": float("nan")}


def off_scale_nsfw(found, answers, monkeypatch):
    answers.nsfw = answers.nsfw | {"a.jpg": -1.0}


def fail_redis(found, answers, monkeypatch):
    monkeypatch.setattr(verdict_cache, "get_scores", MagicMock(side_effect=ConnectionError()))


@pytest.mark.parametrize(
    "failure", [fail_commons, fail_judge, off_scale_need, off_scale_relevance, off_scale_nsfw, fail_redis]
)
async def test_any_failure_is_unavailable(found, answers, monkeypatch, failure):
    failure(found, answers, monkeypatch)
    selection = await search()
    assert (selection.outcome, selection.images) == (ImageOutcome.UNAVAILABLE, [])


async def test_images_that_are_too_new_denied_or_ruled_out_are_never_judged(found, answers, settings):
    recent = datetime.now(timezone.utc)
    found.images = [
        dataclasses.replace(CANDIDATES[0], revised_at=recent),
        CANDIDATES[1],
        dataclasses.replace(CANDIDATES[2], caption="Topless sunbather"),
        dataclasses.replace(CANDIDATES[3], page_url="https://commons.wikimedia.org/wiki/File:D.jpg"),
        dataclasses.replace(CANDIDATES[4], categories=("Deletion requests November 2026",)),
        CANDIDATES[5],
    ]
    settings.IMAGE_SEARCH_DENIED_FILES = {"sha1-of-b.jpg", "https://commons.wikimedia.org/wiki/file:d.jpg"}

    selection = await search()

    assert titles(selection) == ["f.jpg"]
    assert all(len(request["questions"]) <= 2 for request in answers.requests)


async def test_images_late_after_web_ranking_are_unavailable(found, answers, settings):
    settings.IMAGE_SEARCH_GRACE_SECONDS = 0.05
    answers.delay = 0.5

    started = time.monotonic()
    selection = await search()

    assert selection.outcome == ImageOutcome.UNAVAILABLE
    assert time.monotonic() - started < 0.45


async def test_images_may_take_as_long_as_web_ranking(found, answers, settings):
    settings.IMAGE_SEARCH_GRACE_SECONDS = 0.05
    answers.delay = 0.1
    assert (await search(ranking_seconds=0.4)).outcome == ImageOutcome.SHOWN


async def test_a_second_search_reuses_the_nsfw_scores(found, answers):
    await search()
    await search("red pandas")

    nsfw_requests = [request for request in answers.requests if "need" not in request["questions"]]
    assert len(nsfw_requests) == 1
    assert len(answers.requests) == 3
