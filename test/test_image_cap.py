import math

from django.conf import settings

from mwmbl.imagesearch.serving import ServedImagesRanker
from mwmbl.tinysearchengine.image_cap import ImageCapRanker, cap_images
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import score_documents
from mwmbl.tinysearchengine.mmr_rank import MMRRanker, mmr_rerank


def _web(name: str, extract: str = "") -> Document:
    return Document(title=name, url=f"https://{name}.example.com/", extract=extract)


def _image(name: str, extract: str = "") -> Document:
    return Document(title=name, url=f"https://commons.wikimedia.org/wiki/File:{name}.jpg", extract=extract)


def _urls(documents: list[Document]) -> list[str]:
    return [document.url for document in documents]


class _FakeRanker:
    def __init__(self, results: list[Document]):
        self.results = results

    def search(self, s, additional_results, use_external_search=True):
        return self.results

    def retrieve(self, q):
        return q

    def search_retrieved(self, retrieval, additional_results):
        return self.results

    def complete(self, q):
        return ["completion"]

    def get_raw_results(self, query):
        return ["raw"]


def test_images_after_the_cap_are_dropped_and_web_results_move_up():
    results = [_image("a"), _web("one"), _image("b"), _image("c"), _web("two"), _image("d"), _web("three")]

    capped = cap_images(results, 2)

    assert _urls(capped) == _urls([_image("a"), _web("one"), _image("b"), _web("two"), _web("three")])


def test_a_list_within_the_cap_is_unchanged():
    results = [_web("one"), _image("a"), _web("two"), _image("b")]

    assert cap_images(results, 2) == results


def test_a_list_without_images_is_unchanged():
    results = [_web("one"), _web("two"), _web("three")]

    assert cap_images(results, 2) == results


def test_every_commons_file_url_counts_but_other_commons_pages_do_not():
    mobile = Document(title="m", url="https://commons.m.wikimedia.org/wiki/File:M.jpg", extract="")
    index_php = Document(title="i", url="https://commons.wikimedia.org/w/index.php?title=Image:I.png", extract="")
    category = Document(title="c", url="https://commons.wikimedia.org/wiki/Category:Trains", extract="")

    capped = cap_images([_image("a"), mobile, index_php, category], 1)

    assert _urls(capped) == _urls([_image("a"), category])


def test_a_second_url_for_a_kept_file_is_dropped_without_using_a_slot():
    mobile_a = Document(title="a", url="https://commons.m.wikimedia.org/wiki/File:a.jpg", extract="")

    capped = cap_images([_image("a"), mobile_a, _web("one"), _image("b"), _image("c")], 2)

    assert _urls(capped) == _urls([_image("a"), _web("one"), _image("b")])


def test_the_ranker_caps_search_and_search_retrieved_and_delegates_the_rest():
    results = [_image("a"), _image("b"), _image("c"), _web("one")]
    ranker = ImageCapRanker(_FakeRanker(results), 2)

    assert _urls(ranker.search("q", [])) == _urls([_image("a"), _image("b"), _web("one")])
    assert _urls(ranker.search_retrieved(ranker.retrieve("q"), [])) == _urls([_image("a"), _image("b"), _web("one")])
    assert ranker.complete("q") == ["completion"]
    assert ranker.get_raw_results("q") == ["raw"]


def test_the_cap_keeps_the_first_images_in_the_order_mmr_settled_on():
    # Three images in a row on one domain: MMR moves the web results between them, and the cap then keeps the two
    # that MMR left highest, not the two the model scored highest.
    results = [_image("a", "station"), _image("b", "station"), _image("c", "station")] + [
        _web(name) for name in ["one", "two", "three", "four"]
    ]
    diversified = mmr_rerank(results)
    ranker = ImageCapRanker(MMRRanker(_FakeRanker(results)), 2)

    capped = ranker.search("q", [])

    first_two_images = [document for document in diversified if "commons" in document.url][:2]
    assert _urls(capped) == _urls(
        [document for document in diversified if "commons" not in document.url or document in first_two_images]
    )
    assert _urls(capped)[:2] == _urls([_image("a"), _web("one")])


def test_both_served_rankers_are_capped_at_the_setting_outside_the_store_check():
    from mwmbl.search_setup import combined_ranker, ranker

    # Outside ServedImagesRanker, so a file the store doesn't vouch for is dropped before it can use a slot.
    assert isinstance(ranker, ImageCapRanker)
    assert isinstance(ranker.ranker, ServedImagesRanker)
    assert isinstance(ranker.ranker.ranker, MMRRanker)
    assert isinstance(combined_ranker, ImageCapRanker)
    assert isinstance(combined_ranker.ranker, ServedImagesRanker)
    assert ranker.cap == combined_ranker.cap == settings.MAX_IMAGES_PER_RESULT_LIST == 2


def test_the_configured_model_loads_and_scores():
    model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
    documents = [_web("python", "the python programming language"), _image("Python_logo", "the python logo")]

    scores = score_documents(model, "python", documents)

    assert len(scores) == 2
    assert all(math.isfinite(score) for score in scores)
