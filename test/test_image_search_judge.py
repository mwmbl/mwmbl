"""The image judge's NSFW scores: one request for what is not cached, and failing closed (no network)."""

import dataclasses
from unittest.mock import MagicMock

import pytest
import requests
from django.core.cache import cache
from test_image_search import make_image

from mwmbl.imagesearch import judge, prompt
from mwmbl.tinysearchengine import jev_rank

IMAGE = make_image(sha1="abc")
OTHER = dataclasses.replace(IMAGE, title="Other.jpg", sha1="def")


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def stub_judge(monkeypatch):
    """Stub the judge's endpoint: answer each image with the score `score_for` gives its text."""
    requests_made = []

    def configure(score_for):
        def fake_post(url, json, headers, timeout):
            requests_made.append(json)
            answers = {
                key: {"score": score_for(question["instructions"]["image"])}
                for key, question in json["questions"].items()
            }
            response = MagicMock()
            response.json.return_value = {"answers": answers}
            return response

        monkeypatch.setattr(jev_rank.session, "post", fake_post)
        return requests_made

    return configure


def score_by_title(text: str) -> float:
    return 2.5 if "Other.jpg" in text else 0.25


def test_nsfw_scores_are_in_image_order_and_asked_in_one_request(stub_judge):
    requests_made = stub_judge(score_by_title)

    assert judge.nsfw_scores([OTHER, IMAGE]) == [2.5, 0.25]
    assert len(requests_made) == 1
    assert requests_made[0]["state"] == {"policy": prompt.NSFW_POLICY}


def test_cached_scores_are_not_asked_again(stub_judge):
    requests_made = stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])

    assert judge.nsfw_scores([IMAGE, OTHER]) == [0.25, 2.5]
    asked_texts = [question["instructions"]["image"] for question in requests_made[1]["questions"].values()]
    assert asked_texts == [prompt.image_text(OTHER)]


@pytest.mark.parametrize(
    "changed",
    [
        dataclasses.replace(IMAGE, sha1="replaced"),
        dataclasses.replace(IMAGE, description="A red panda, now described differently"),
    ],
)
def test_a_new_revision_or_new_text_is_asked_afresh(stub_judge, changed):
    requests_made = stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])

    judge.nsfw_scores([changed])

    assert len(requests_made) == 2


def test_a_new_model_is_asked_afresh(stub_judge, settings):
    requests_made = stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])

    settings.JEV_MODEL = "stub_judge-next"
    judge.nsfw_scores([IMAGE])

    assert len(requests_made) == 2
    assert requests_made[1]["model"] == "stub_judge-next"


@pytest.mark.parametrize("bad_score", [-0.5, 3.5, float("nan"), None, "0", True])
def test_a_malformed_score_raises_and_nothing_is_cached(stub_judge, bad_score):
    requests_made = stub_judge(lambda text: bad_score if "Other.jpg" in text else 0.25)

    with pytest.raises(ValueError):
        judge.nsfw_scores([IMAGE, OTHER])

    stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])
    assert len(requests_made) == 2


def test_a_failed_request_raises(monkeypatch):
    def failing_post(url, json, headers, timeout):
        raise requests.Timeout()

    monkeypatch.setattr(jev_rank.session, "post", failing_post)

    with pytest.raises(requests.Timeout):
        judge.nsfw_scores([IMAGE])


def test_an_http_error_raises_and_nothing_is_cached(monkeypatch, stub_judge):
    refused = MagicMock()
    refused.raise_for_status.side_effect = requests.HTTPError("401")
    monkeypatch.setattr(jev_rank.session, "post", lambda url, json, headers, timeout: refused)

    with pytest.raises(requests.HTTPError):
        judge.nsfw_scores([IMAGE])

    requests_made = stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])
    assert len(requests_made) == 1


def test_a_missing_answer_raises(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"answers": {"n0": {"score": 0.0}}}
    monkeypatch.setattr(jev_rank.session, "post", lambda url, json, headers, timeout: response)

    with pytest.raises(KeyError):
        judge.nsfw_scores([IMAGE, OTHER])


def test_nothing_is_asked_when_every_score_is_cached_or_there_are_no_images(stub_judge):
    requests_made = stub_judge(score_by_title)
    judge.nsfw_scores([IMAGE])

    assert judge.nsfw_scores([IMAGE]) == [0.25]
    assert judge.nsfw_scores([]) == []
    assert len(requests_made) == 1
