"""The Seed Search images harness: Jev request builders and answer parsing (no network)."""

import dataclasses
import json
from datetime import datetime, timezone

from mwmbl.imagesearch.types import SourceImage
from mwmbl.rankeval.seed_images import jev_images
from mwmbl.rankeval.seed_images.collect import image_from_json, image_to_json
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import RELEVANCE_QUESTION, SEARCHER

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


def test_image_text_matches_the_benchmark_format():
    long_image = dataclasses.replace(IMAGE, description="x" * (jev_images.DESCRIPTION_CHARS + 10))

    assert (
        jev_images.image_text(IMAGE)
        == f"File name: Red panda.jpg\nDescription: {IMAGE.description}\nCategories: {CATEGORIES}"
    )
    assert jev_images.image_text(long_image).count("x") == jev_images.DESCRIPTION_CHARS


def test_nsfw_request_asks_one_question_per_image_under_the_policy():
    request = jev_images.nsfw_request([IMAGE, IMAGE])

    assert request["state"] == {"policy": jev_images.NSFW_POLICY}
    assert list(request["questions"]) == ["n0", "n1"]
    assert request["questions"]["n1"]["instructions"] == {
        "image": jev_images.image_text(IMAGE),
        "question": jev_images.NSFW_QUESTION,
    }
    assert jev_images.scores({"answers": {"n1": {"score": 2.5}, "n0": {"score": 0.1}}}, "n", 2) == [0.1, 2.5]


def test_need_request_numbers_the_web_titles():
    request = jev_images.need_request("red panda", ["Red panda - Wikipedia", "Red Panda Network"])

    assert request["state"] == {"query": "red panda", "searcher": SEARCHER}
    instructions = request["questions"]["need"]["instructions"]
    assert instructions == {
        "results": "1. Red panda - Wikipedia\n2. Red Panda Network",
        "question": jev_images.IMAGE_NEED_QUESTION,
    }
    assert jev_images.need_score({"answers": {"need": {"score": 1.75}}}) == 1.75


def test_combined_request_holds_the_web_image_and_need_questions():
    documents = [Document("Red panda", "https://en.wikipedia.org/wiki/Red_panda", "A mammal")]

    request = jev_images.combined_request("red panda", documents, ["Red panda"], [IMAGE, IMAGE])

    assert sorted(request["questions"]) == ["i0", "i1", "n0", "n1", "need", "q0", "r0"]
    assert request["questions"]["r0"]["instructions"]["question"] == RELEVANCE_QUESTION
    assert request["state"] == {"query": "red panda", "searcher": SEARCHER, "policy": jev_images.NSFW_POLICY}
