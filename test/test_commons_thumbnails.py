import pytest
from django.conf import settings

from mwmbl.imagesearch.commons import THUMBNAIL_WIDTH
from mwmbl.imagesearch.display import build_image_strip, image_for_result, thumbnail_url
from mwmbl.tinysearchengine.indexer import Document, DocumentSource

PREFIX = "https://upload.wikimedia.org/wikipedia/commons/thumb"
LONG_NAME = (
    "Conquering_the_wilderness;_or,_New_pictorial_history_of_the_life_and_times_of_the_pioneer_heroes_and_"
    "heroines_of_America,_a_full_account_of_the_romantic_deeds,_lofty_achievements,_and_marvellous_"
    "(14748793216).jpg"
)

# Names, and the thumbnail directory Commons itself gave for each in Seed Search candidates.
REAL_EXAMPLES = [
    ("Lilly_mounjaro_KwikPen_Tirzepatid_5_mg_per_dose_rate-9442.jpg", "a/a3"),
    ("Tirzepatide_GLP1R_complex_7rgp.png", "2/20"),
    ("Storage_Tank_Weld_County_Colorado_(229820827).jpeg", "f/f5"),
    ("Historical_income_tax_rates_marginal_rates.webp", "9/99"),
]


@pytest.mark.parametrize("file_name, directory", REAL_EXAMPLES)
def test_the_thumbnail_directory_is_from_the_name_hash(file_name, directory):
    url = thumbnail_url(file_name)

    assert url.startswith(f"{PREFIX}/{directory}/")


def test_brackets_are_escaped_as_commons_does():
    url = thumbnail_url("Storage_Tank_Weld_County_Colorado_(229820827).jpeg")

    escaped = "Storage_Tank_Weld_County_Colorado_%28229820827%29.jpeg"
    assert url == f"{PREFIX}/f/f5/{escaped}/330px-{escaped}"


def test_a_non_ascii_name_is_hashed_as_utf8_and_escaped():
    url = thumbnail_url("2020_Eum_Moon-suk_음문석.jpg")

    escaped = "2020_Eum_Moon-suk_%EC%9D%8C%EB%AC%B8%EC%84%9D.jpg"
    assert url == f"{PREFIX}/b/bc/{escaped}/330px-{escaped}"


def test_an_svg_thumbnail_is_a_png():
    url = thumbnail_url("Leeds_UK_location_map.svg")

    assert url.endswith("/330px-Leeds_UK_location_map.svg.png")


def test_a_long_name_gets_the_generic_thumbnail_name():
    url = thumbnail_url(LONG_NAME)

    assert url.startswith(f"{PREFIX}/f/f5/")
    assert url.endswith("/330px-thumbnail.jpg")


def test_an_uppercase_extension_gives_a_lowercase_generic_name():
    url = thumbnail_url("A" * 170 + ".JPG")

    assert url.endswith("/330px-thumbnail.jpg")


def test_a_long_svg_has_no_thumbnail_because_its_name_is_not_confirmed():
    assert thumbnail_url("A" * 170 + ".svg") is None


@pytest.mark.parametrize("file_name", ["Scan.tif", "Paper.pdf", "Book.djvu", "Clip.webm", "Clip.ogv", "No_extension"])
def test_an_unsupported_format_has_no_thumbnail(file_name):
    assert thumbnail_url(file_name) is None


@pytest.mark.parametrize("file_name", ["png", "jpg", ".jpg"])
def test_a_name_with_no_stem_before_the_dot_has_no_thumbnail(file_name):
    assert thumbnail_url(file_name) is None


def test_seed_search_asks_for_the_width_the_derived_urls_use():
    assert f"/{THUMBNAIL_WIDTH}px-" in thumbnail_url("Cat.jpg")


def _commons(name: str, source: DocumentSource | None = DocumentSource.COMMONS_CC_BY_SA_4_0) -> Document:
    return Document(name, f"https://commons.wikimedia.org/wiki/File:{name}", "A description", source=source)


def _web(name: str) -> Document:
    return Document(name, f"https://{name}.example.com/", "A page")


def test_an_indexed_commons_file_is_an_image_with_its_licence():
    image = image_for_result(_commons("Tirzepatide_GLP1R_complex_7rgp.png"))

    assert image.model_dump() == {
        "title": "Tirzepatide GLP1R complex 7rgp.png",
        "page_url": "https://commons.wikimedia.org/wiki/File:Tirzepatide_GLP1R_complex_7rgp.png",
        "thumbnail_url": f"{PREFIX}/2/20/Tirzepatide_GLP1R_complex_7rgp.png/330px-Tirzepatide_GLP1R_complex_7rgp.png",
        "source": "commons",
        "license_name": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    }


def test_a_licence_with_no_page_has_no_licence_url():
    assert image_for_result(_commons("Cat.jpg", DocumentSource.COMMONS_OTHER)).license_url == (
        "https://commons.wikimedia.org/wiki/File:Cat.jpg"
    )


@pytest.mark.parametrize(
    "document",
    [
        _commons("Cat.jpg", None),
        _commons("Cat.jpg", DocumentSource.STAAN),
        _commons("Scan.tif"),
        Document("A", "https://example.com/File:Cat.jpg", "x", source=DocumentSource.COMMONS_CC0),
    ],
)
def test_a_result_that_is_not_a_vetted_file_with_a_thumbnail_is_not_an_image(document):
    """A crawled page has no source because nothing vetted it, and a format with no thumbnail can't be shown."""
    assert image_for_result(document) is None


def test_the_strip_takes_the_top_ten_commons_files_in_rank_order_and_leaves_the_rest():
    results = [_web("a"), _commons("One.jpg"), _web("b"), _commons("Two.jpg"), _commons("Scan.tif"), _web("c")]

    images, web_results = build_image_strip(results)

    assert [image.title for image in images] == ["One.jpg", "Two.jpg"]
    assert [result.title for result in web_results] == ["a", "b", "Scan.tif", "c"]


def test_the_strip_is_at_most_its_size():
    results = [_commons(f"File{i}.jpg") for i in range(6)]

    images, web_results = build_image_strip(results)

    assert len(images) == settings.IMAGES_DISPLAY_COUNT
    assert [result.title for result in web_results] == ["File4.jpg", "File5.jpg"]


def test_a_file_below_the_top_ten_stays_a_web_result():
    results = [_web(f"w{i}") for i in range(10)] + [_commons("Late.jpg")]

    images, web_results = build_image_strip(results)

    assert images == []
    assert web_results == results


def test_a_file_in_the_strip_twice_is_shown_once_and_dropped_from_the_web_results():
    results = [_commons("One.jpg"), _commons("One.jpg", DocumentSource.COMMONS_CC0), _web("a")]

    images, web_results = build_image_strip(results)

    assert len(images) == 1
    assert [result.title for result in web_results] == ["a"]


def test_a_crawled_commons_page_of_another_file_stays_a_web_result():
    results = [_commons("Crawled.jpg", None), _commons("One.jpg")]

    images, web_results = build_image_strip(results)

    assert [image.title for image in images] == ["One.jpg"]
    assert [result.title for result in web_results] == ["Crawled.jpg"]


@pytest.mark.django_db
def test_building_the_strip_makes_no_database_query(django_assert_num_queries):
    with django_assert_num_queries(0):
        images, _ = build_image_strip([_commons("Cat.jpg")])

    assert len(images) == 1
