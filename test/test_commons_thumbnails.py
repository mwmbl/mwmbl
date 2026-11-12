import pytest

from mwmbl.imagesearch.display import thumbnail_url

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
