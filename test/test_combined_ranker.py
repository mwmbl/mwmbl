"""CombinedLTRRanker: Combined Search's ranker tells the model what Staan made of each candidate."""

from pandas import DataFrame

from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.staan import staan_score

QUERY = "rust programming"


def staan_result(url: str, title: str, rank: int) -> Document:
    return Document(title, url, "", staan_score(rank), source=DocumentSource.STAAN)


def index_result(url: str, title: str) -> Document:
    return Document(title, url, "", 1.0)


def ranker(model=None) -> CombinedLTRRanker:
    return CombinedLTRRanker(None, None, model)


def test_staan_rank_is_keyed_on_the_url_and_read_from_the_score():
    results = [
        staan_result("https://a.com/", "Rust", 0),
        staan_result("https://b.com/", "Rust", 3),
        index_result("https://b.com/", "Rust"),
        index_result("https://c.com/", "Rust"),
    ]

    records = ranker().records(QUERY, results)

    assert [r["staan_rank"] for r in records] == [0, 3, 3, None]
    assert [r["from_staan"] for r in records] == [True, True, False, False]
    assert all(r["staan_asked"] for r in records)


def test_an_index_only_pool_counts_as_staan_not_asked():
    records = ranker().records(QUERY, [index_result("https://c.com/", "Rust")])

    assert records[0]["staan_asked"] is False
    assert records[0]["staan_rank"] is None


def test_only_staan_results_escape_the_term_filter():
    training = [
        {"query": QUERY, "url": f"https://{i}.com/", "title": "Rust Programming", "extract": "", "score": 1.0}
        for i in range(20)
    ]
    model = RustXGBPipeline(num_rounds=5, provider_features=True)
    model.fit(DataFrame(training), [1.0] * 20)

    staan_off_topic = staan_result("https://bread.com/", "Baking bread", 0)
    index_off_topic = index_result("https://cakes.com/", "Baking cakes")
    index_on_topic = index_result("https://rust-lang.org/", "Rust Programming")

    ordered = ranker(model).order_results(QUERY.split(), [staan_off_topic, index_off_topic, index_on_topic], True)

    assert {page.url for page in ordered} == {staan_off_topic.url, index_on_topic.url}
