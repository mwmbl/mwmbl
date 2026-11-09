"""The Seed Search images labelling: Jev-safe filtering, the relevance request, sampling and label parsing."""

import dataclasses
import random

import pytest

from mwmbl.imagesearch.pipeline import file_id
from mwmbl.rankeval.seed_images import jev_images
from mwmbl.rankeval.seed_images.gate_data import below_threshold
from mwmbl.rankeval.seed_images.label_batches import chunks, stratified_sample
from mwmbl.rankeval.seed_images.labels import parse_out
from test.test_seed_images_eval import IMAGE

OTHER = dataclasses.replace(IMAGE, title="Other.jpg", sha1="def")


def test_below_threshold_drops_images_at_or_above_it():
    nsfw_by_file = {file_id(IMAGE): 0.99, file_id(OTHER): 1.0}

    assert below_threshold([IMAGE, OTHER], nsfw_by_file) == [IMAGE]


def test_relevance_request_asks_about_each_image_for_the_query():
    request = jev_images.relevance_request("red panda", [IMAGE, OTHER])

    assert request["state"]["query"] == "red panda"
    assert list(request["questions"]) == ["i0", "i1"]


def test_stratified_sample_takes_equally_from_each_band():
    scores = {f"q{i}": float(i) for i in range(10)}

    sample = stratified_sample(scores, 2, 4, random.Random(0))

    assert sorted(scores[key] < 5 for key in sample) == [False, False, True, True]


def test_chunks():
    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]


def test_parse_out_reads_valid_lines():
    text = '{"id": "N1", "need": 2, "reason": "landmark"}\n\n{"id": "N2", "need": 0, "reason": "site"}\n'

    labels = parse_out(text, {"N1", "N2"}, "need", ("reason",))

    assert labels == {"N1": {"need": 2, "reason": "landmark"}, "N2": {"need": 0, "reason": "site"}}


@pytest.mark.parametrize(
    "text",
    [
        '{"id": "N1", "need": 4, "reason": "x"}\n{"id": "N2", "need": 0, "reason": "x"}',
        '{"id": "N1", "need": 1, "reason": "x"}\n{"id": "N1", "need": 1, "reason": "x"}',
        '{"id": "N1", "need": 1, "reason": "x"}\n{"id": "N3", "need": 1, "reason": "x"}',
        '{"id": "N1", "need": 1, "reason": "x"}',
        '{"id": "N1", "need": 1}\n{"id": "N2", "need": 0, "reason": "x"}',
    ],
)
def test_parse_out_rejects_bad_output(text):
    with pytest.raises((AssertionError, KeyError)):
        parse_out(text, {"N1", "N2"}, "need", ("reason",))
