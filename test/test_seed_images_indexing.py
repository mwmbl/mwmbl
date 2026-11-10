"""Indexing Seed Search's Commons images as pages of their own (no network, no index)."""

import dataclasses

from test_seed_images_eval import IMAGE

from mwmbl.rankeval.seed_images import indexing
from mwmbl.tinysearchengine.indexer import Document

COMMONS = "https://commons.wikimedia.org/wiki/File:"


def commons_image(**fields):
    return dataclasses.replace(IMAGE, **fields)


def test_an_image_is_indexed_by_its_file_name_and_description():
    image = commons_image(title="Big_Ben at night.jpg", description="The clock tower.", categories=("CC-BY-SA-4.0",))

    document = indexing.image_document(image, with_categories=False)

    assert (document.title, document.url, document.extract) == ("Big Ben at night", image.page_url, "The clock tower.")


def test_categories_follow_the_description_and_the_extract_is_cut_as_the_crawler_cuts_it():
    image = commons_image(description="Tower.", categories=("Clocks", "x" * 200))

    extract = indexing.image_document(image, with_categories=True).extract

    assert extract.startswith("Tower. · Clocks · x") and len(extract) == 155 and extract.endswith("…")


def test_an_image_without_a_description_has_its_categories_alone():
    image = commons_image(description="", categories=("Clocks",))

    assert indexing.image_document(image, with_categories=True).extract == "Clocks"


def test_top_ten_change_finds_images_and_the_web_results_they_displace():
    original = [{"url": "https://a.com/"}, {"url": "https://b.com/"}]
    augmented = [{"url": "https://a.com/"}, {"url": COMMONS + "X.jpg"}]

    change = indexing.top_ten_change(original, augmented)

    assert change == {"images": [(2, COMMONS + "X.jpg")], "displaced": ["https://b.com/"], "changed": True}


def test_same_term_writing_keeps_only_the_terms_a_page_already_holds():
    held = {(7, "clock", "https://a.com/")}
    clock = Document("Clock", COMMONS + "C.jpg", "", term="clock")
    tower = Document("Tower", COMMONS + "T.jpg", "", term="tower")

    assert indexing.held_terms({7: [clock, tower], 8: [tower]}, held) == {7: [clock]}


def test_an_unchanged_top_ten_is_not_a_change():
    results = [{"url": "https://a.com/"}]

    assert indexing.top_ten_change(results, results) == {"images": [], "displaced": [], "changed": False}
