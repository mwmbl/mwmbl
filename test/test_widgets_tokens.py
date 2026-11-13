import random
from itertools import combinations

import pytest

from mwmbl.widgets.keys import MAX_QUERY_TOKENS, query_keys, storage_key
from mwmbl.widgets.tokens import MAX_QUERY_CHARS, widget_tokens


@pytest.mark.parametrize(
    "query, expected",
    [
        ("10km in miles", [":n", "km", "miles"]),
        ("10 KM to Miles", [":n", "km", "miles"]),
        ("$50 to eur", [":money", "eur"]),
        ("50£ in usd", [":money", "usd"]),
        ("$1,000.50 to gbp", [":money", "gbp"]),
        ("20% of 150", [":n", "%"]),
        ("20 % of 150", [":n", "%"]),
        ("days until 2026-12-25", ["days", "until", ":date"]),
        ("days until 25/12/2026", ["days", "until", ":date"]),
        ("time difference 9:30pm to 14:05", ["time", "difference", ":time"]),
        ("meeting at 5pm", ["meeting", ":time"]),
        ("weather in new york", ["weather", "new", "york"]),
        ("what's the weather in London?", ["weather", "london"]),
        ("co2 emissions", ["co2", "emissions"]),
        ("3.5 miles", [":n", "miles"]),
        ("", []),
        ("in of the", []),
    ],
)
def test_widget_tokens(query, expected):
    assert widget_tokens(query) == expected


def test_a_placeholder_typed_in_the_query_is_not_one():
    assert widget_tokens(":date :money") == ["date", "money"]


def test_a_long_query_is_cut():
    tokens = widget_tokens("word " * MAX_QUERY_CHARS + "after")
    assert tokens == ["word"]


def test_storage_key_is_a_sorted_pair():
    tokens = {"weather", "new", "york"}
    assert storage_key(tokens, "weather", "york") == "widget:weather york"
    assert storage_key(tokens, "york", "weather") == "widget:weather york"


def test_storage_key_of_a_single_token():
    assert storage_key({"calculator"}, "calculator", "calculator") == "widget:calculator"


def test_storage_key_rejects_tokens_outside_the_trigger_tokens():
    with pytest.raises(ValueError):
        storage_key({"weather", "york"}, "weather", "paris")
    with pytest.raises(ValueError):
        storage_key({"weather", "york"}, "york", "york")


def test_query_keys_are_singles_then_pairs():
    assert query_keys(["weather", "london"]) == ["widget:weather", "widget:london", "widget:london weather"]


def test_query_keys_cap_at_21_reads():
    keys = query_keys([f"t{i}" for i in range(20)])
    assert len(keys) == MAX_QUERY_TOKENS + len(list(combinations(range(MAX_QUERY_TOKENS), 2))) == 21
    assert len(set(keys)) == 21


def test_a_matching_documents_key_is_among_the_query_keys():
    rng = random.Random(317)
    vocabulary = [":n", "%", "km", "weather", "york", "new", "miles", ":date", "days"]
    for _ in range(500):
        trigger_tokens = rng.sample(vocabulary, rng.randint(1, 4))
        extra_tokens = rng.sample(vocabulary, rng.randint(0, 2))
        query = list(dict.fromkeys(trigger_tokens + extra_tokens))
        if len(query) > MAX_QUERY_TOKENS:
            continue
        rng.shuffle(query)
        trigger_word, anchor = rng.sample(trigger_tokens, 2) if len(trigger_tokens) > 1 else trigger_tokens * 2
        assert storage_key(set(trigger_tokens), trigger_word, anchor) in query_keys(query)
