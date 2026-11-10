"""The Seed Search images harness: Jev request builders and answer parsing (no network)."""

import json
from datetime import datetime, timezone

import pytest

from mwmbl.imagesearch import judge, prompt
from mwmbl.imagesearch.commons import SourceRejectedRequest
from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage
from mwmbl.rankeval.seed_images import collect, jev_images
from mwmbl.rankeval.seed_images.collect import image_from_json, image_to_json, with_backoff
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import RELEVANCE_QUESTION

CATEGORIES = "Ailurus fulgens | Zoos"
IMAGE = SourceImage(
    title="Red panda.jpg",
    page_url="https://commons.wikimedia.org/wiki/File:Red_panda.jpg",
    thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Red_panda.jpg/320px-Red_panda.jpg",
    thumbnail_width=320,
    thumbnail_height=213,
    description="A red panda in a tree",
    categories=("Ailurus fulgens", "Zoos"),
    license_name="CC BY-SA 4.0",
    license_url="https://creativecommons.org/licenses/by-sa/4.0",
    attribution="Someone",
    sha1="abc",
    uploaded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    rank=1,
)


def test_image_json_round_trips():
    assert image_from_json(json.loads(json.dumps(image_to_json(IMAGE)))) == IMAGE


def test_with_backoff_retries_an_unavailable_source(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda seconds: None)
    outcomes = [ImageSourceUnavailable("rate limited"), "images"]

    def fetch():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result, _ = with_backoff(fetch)
    assert result == "images"


def test_with_backoff_raises_a_rejected_request_at_once(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda seconds: pytest.fail("a rejected request was retried"))
    calls = []

    def fetch():
        calls.append(1)
        raise SourceRejectedRequest("bad request")

    with pytest.raises(SourceRejectedRequest):
        with_backoff(fetch)
    assert len(calls) == 1


def test_nsfw_request_asks_one_question_per_image_under_the_policy():
    request = judge.nsfw_request([IMAGE, IMAGE])

    assert request["state"] == {"policy": prompt.NSFW_POLICY}
    assert list(request["questions"]) == ["n0", "n1"]
    assert request["questions"]["n1"]["instructions"] == {
        "image": prompt.image_text(IMAGE),
        "question": prompt.NSFW_QUESTION,
    }
    assert judge.scores({"answers": {"n1": {"score": 2.5}, "n0": {"score": 0.1}}}, "n", 2) == [0.1, 2.5]


def test_combined_request_holds_the_web_image_and_need_questions():
    documents = [Document("Red panda", "https://en.wikipedia.org/wiki/Red_panda", "A mammal")]

    request = jev_images.combined_request("red panda", documents, documents, [IMAGE, IMAGE])

    assert sorted(request["questions"]) == ["i0", "i1", "n0", "n1", "need", "q0", "r0"]
    assert request["questions"]["r0"]["instructions"]["question"] == RELEVANCE_QUESTION
    assert request["state"] == judge.query_state("red panda", documents) | {"policy": prompt.NSFW_POLICY}
