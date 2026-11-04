"""
Small helpers that decide what is stored or counted: tagging documents with the term whose
page they live on, the dataset counters, and the domain-link filters.
"""

from datetime import date
from types import SimpleNamespace

import fakeredis
import pytest

from mwmbl.crawler import domains
from mwmbl.crawler.domains import DomainLinkDatabase
from mwmbl.crawler.stats import DATASET_QUERIES_COUNT_KEY, DATASET_RESULTS_COUNT_KEY, StatsManager
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.utils import add_term_info, add_term_infos, batch, get_domain


def test_batch_splits_into_chunks_with_a_short_last_one():
    assert list(batch([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(batch([], 3)) == []


def test_get_domain_requires_a_scheme():
    assert get_domain("https://www.example.com/page") == "www.example.com"
    with pytest.raises(ValueError):
        get_domain("example.com")


class FakeIndex:
    """An index where every token lives on a page decided by the token's first letter."""

    def get_key_page_index(self, token: str) -> int:
        return ord(token[0]) % 7


def _document(**fields) -> Document:
    return Document(
        title="Pasta recipes", url="https://example.com/pasta", extract="Cooking pasta", score=1.0, **fields
    )


def test_a_document_is_tagged_with_a_term_stored_on_its_page():
    index = FakeIndex()
    document = _document()
    tokens = ["pasta", "recipes", "cooking", "example"]
    page_index = index.get_key_page_index("recipes")

    tagged = add_term_info(document, index, page_index)

    assert tagged.term in tokens
    assert index.get_key_page_index(tagged.term) == page_index
    assert tagged.url == document.url


def test_a_document_with_no_term_on_the_page_is_an_error():
    with pytest.raises(ValueError, match="Could not find token"):
        add_term_info(_document(), FakeIndex(), page_index=-1)


def test_documents_without_a_term_are_tagged_and_unmatched_ones_dropped():
    index = FakeIndex()
    already = _document(term="existing")
    page_index = index.get_key_page_index("recipes")
    untaggable = Document(title="", url="", extract="", score=1.0)

    result = list(add_term_infos([already, _document(), untaggable], index, page_index))

    assert result[0] is already
    assert len(result) == 2
    assert result[1].term is not None


def _dataset(day: str, successes: list[int]):
    results = [SimpleNamespace(success=True, results=[None] * n) for n in successes]
    results.append(SimpleNamespace(success=False, results=[None] * 50))
    return SimpleNamespace(date=day, queryDataset=[None] * 3, searchResults=results)


def test_dataset_submissions_count_queries_and_only_successful_results():
    redis = fakeredis.FakeStrictRedis()

    StatsManager(redis).record_dataset(_dataset("2024-03-05", [2, 5]))

    day = date(2024, 3, 5)
    assert int(redis.get(DATASET_QUERIES_COUNT_KEY.format(date=day))) == 3
    assert int(redis.get(DATASET_RESULTS_COUNT_KEY.format(date=day))) == 7


def test_a_dataset_with_an_unreadable_date_is_counted_today(monkeypatch):
    redis = fakeredis.FakeStrictRedis()
    monkeypatch.setattr("mwmbl.crawler.stats.utc_today", lambda: date(2030, 1, 1))

    StatsManager(redis).record_dataset(_dataset("not-a-date", [1]))

    assert int(redis.get(DATASET_QUERIES_COUNT_KEY.format(date=date(2030, 1, 1)))) == 3


@pytest.fixture
def bloom_files(settings, tmp_path):
    settings.DOMAIN_LINKS_BLOOM_FILTER_PATH = str(tmp_path / "links_{domain_group}.bloom")
    settings.NUM_DOMAINS_IN_BLOOM_FILTER = 1000


def test_links_from_a_top_domain_count_towards_the_top_group(bloom_files, monkeypatch):
    monkeypatch.setattr(domains, "TOP_DOMAINS", {"top.example"})
    monkeypatch.setattr(domains, "OTHER_DOMAINS", {"other.example"})

    with DomainLinkDatabase() as database:
        database.update_domain_links("top.example", {"a.example"})
        database.update_domain_links("other.example", {"b.example"})
        database.update_domain_links("unknown.example", {"c.example"})

        assert database.get_domain_score("a.example") == 5
        assert database.get_domain_score("b.example") == 1
        assert database.get_domain_score("c.example") == 0


def test_link_filters_persist_between_openings(bloom_files, monkeypatch):
    monkeypatch.setattr(domains, "TOP_DOMAINS", {"top.example"})
    with DomainLinkDatabase() as database:
        database.update_domain_links("top.example", {"a.example"})

    with DomainLinkDatabase() as reopened:
        assert reopened.get_domain_score("a.example") == 5
