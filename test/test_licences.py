import pytest

from mwmbl.format import get_result_source
from mwmbl.imagesearch.licences import LICENCE_NAMES_AND_URLS, LICENCES, licence_source
from mwmbl.tinysearchengine.indexer import Document, DocumentSource


@pytest.mark.parametrize(
    "name, source",
    [
        ("CC BY-SA 4.0", DocumentSource.COMMONS_CC_BY_SA_4_0),
        ("CC BY 2.5", DocumentSource.COMMONS_CC_BY_2_5),
        ("Public domain", DocumentSource.COMMONS_PUBLIC_DOMAIN),
        ("  CC0 ", DocumentSource.COMMONS_CC0),
        ("CC  BY-SA 3.0", DocumentSource.COMMONS_CC_BY_SA_3_0),
    ],
)
def test_a_listed_licence_maps_to_its_source(name, source):
    assert licence_source(name) == source


@pytest.mark.parametrize("name", ["CC BY-SA 3.0 de", "GFDL 1.2", "No restrictions", ""])
def test_any_other_licence_is_other(name):
    assert licence_source(name) == DocumentSource.COMMONS_OTHER


def test_every_commons_source_has_a_name_and_each_listed_licence_names_itself():
    commons_sources = {source for source in DocumentSource if source >= DocumentSource.COMMONS_PUBLIC_DOMAIN}

    assert set(LICENCE_NAMES_AND_URLS) == commons_sources
    assert all(LICENCE_NAMES_AND_URLS[source][0] == name for name, (source, _, _) in LICENCES.items())


def test_a_licensed_file_reports_commons_as_its_source():
    result = Document("Cat", "https://commons.wikimedia.org/wiki/File:Cat.jpg", "", source=DocumentSource.COMMONS_CC0)

    assert get_result_source(result) == "commons"
