"""The arms of the Seed Search images holistic comparison (no network)."""

import dataclasses

from mwmbl.imagesearch.pipeline import file_id
from mwmbl.rankeval.seed_images import holistic_arms
from test.test_seed_images_eval import IMAGE

GATE = {"num_images": 2, "min_jev_relevance": 1.5}


def image(number: int):
    return dataclasses.replace(
        IMAGE, title=f"{number}.jpg", page_url=f"https://commons/{number}", sha1=str(number), rank=number
    )


def web(count: int) -> list[dict]:
    return [{"url": f"https://{i}.com/", "title": str(i), "extract": ""} for i in range(count)]


def test_an_image_scores_as_a_substantive_page_staan_did_not_return():
    assert holistic_arms.image_composite(3.0) == 2.5


def test_images_are_ranked_by_jev_relevance_then_commons_rank():
    images = [image(1), image(2), image(3)]
    relevance = dict(zip(map(file_id, images), [1.0, 2.0, 2.0]))

    ranked = holistic_arms.ranked_images(images[::-1], relevance)

    assert [shown.rank for shown, _ in ranked] == [2, 3, 1]


def test_the_strip_goes_after_the_top_result_and_displaces_nothing():
    scored = [(image(1), 2.5), (image(2), 2.0), (image(3), 1.9), (image(4), 1.0)]

    results = holistic_arms.with_strip(web(10), scored, GATE)

    assert len(results) == 11 and results[0]["url"] == "https://0.com/"
    assert [shown["title"] for shown in results[1]["images"]] == ["1.jpg", "2.jpg"]
    assert holistic_arms.with_strip(web(10), [(image(4), 1.0)], GATE) == web(10)


def test_option_1_merges_the_two_best_images_by_composite_web_first_on_a_tie():
    composites = [4.0, 3.0, 2.0, 1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4]
    scored = [(image(1), 2.5), (image(2), 1.5), (image(3), 1.4)]

    urls = [result["url"] for result in holistic_arms.merged(web(10), composites, scored)]

    assert urls[:6] == [
        *(f"https://{i}.com/" for i in range(3)),
        "https://commons/1",
        "https://3.com/",
        "https://commons/2",
    ]
    assert len(urls) == 10
