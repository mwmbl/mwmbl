"""The gold ranking a query is scored against.

The rankings dataset holds several scrapes of the same query, taken on different dates
and stored as consecutive rows under the one query - 1,135 of the 5,969 test queries.
Every harness that scores against it goes through gold_scores_for, so this is where the
"one query, one ranking" rule has to hold.
"""
import pandas as pd

from mwmbl.rankeval.evaluation.evaluate import (
    CLICK_PROPORTIONS, gold_scores_for, latest_ranking)


def _rankings(rows):
    return pd.DataFrame(rows, columns=["query", "url", "rank", "date_retrieved"])


def test_latest_ranking_keeps_only_the_most_recent_scrape():
    rankings = _rankings([
        ("piles", "https://old-first", 1, "2025-07-20"),
        ("piles", "https://old-second", 2, "2025-07-20"),
        ("piles", "https://new-first", 1, "2026-01-29"),
        ("piles", "https://new-second", 2, "2026-01-29"),
    ])

    assert list(latest_ranking(rankings)["url"]) == ["https://new-first", "https://new-second"]


def test_latest_ranking_sorts_by_rank():
    rankings = _rankings([
        ("piles", "https://second", 2, "2026-01-29"),
        ("piles", "https://first", 1, "2026-01-29"),
    ])

    assert list(latest_ranking(rankings)["url"]) == ["https://first", "https://second"]


def test_gold_scores_ignore_an_earlier_scrape():
    # Without this the first ten rows would be the older scrape's, and a URL appearing in
    # both would take the weight of whichever row came second - the *worse* rank.
    rankings = _rankings([
        ("piles", "https://nhs", 5, "2025-07-20"),
        ("piles", "https://nhs", 1, "2026-01-29"),
        ("piles", "https://mayo", 2, "2026-01-29"),
    ])

    assert gold_scores_for(rankings) == {
        "https://nhs": CLICK_PROPORTIONS[0],
        "https://mayo": CLICK_PROPORTIONS[1],
    }


def test_gold_scores_drop_a_duplicate_url_keeping_the_best_rank():
    rankings = _rankings([
        ("piles", "https://nhs", 1, "2026-01-29"),
        ("piles", "https://mayo", 2, "2026-01-29"),
        ("piles", "https://nhs", 3, "2026-01-29"),
    ])

    assert gold_scores_for(rankings) == {
        "https://nhs": CLICK_PROPORTIONS[0],
        "https://mayo": CLICK_PROPORTIONS[1],
    }


def test_gold_scores_take_at_most_ten_results():
    rankings = _rankings([("piles", f"https://result-{i}", i, "2026-01-29")
                          for i in range(1, 16)])

    scores = gold_scores_for(rankings)
    assert len(scores) == len(CLICK_PROPORTIONS)
    assert scores["https://result-1"] == CLICK_PROPORTIONS[0]
    assert "https://result-11" not in scores
