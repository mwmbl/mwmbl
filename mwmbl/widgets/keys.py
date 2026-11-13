"""The index keys widget documents are filed under.

A widget document is stored under exactly one key, built from two of its trigger tokens (or
its one token): the spec's trigger word and the instance's most distinctive token, such as
`widget:weather york`. A query has all of a matching widget's trigger tokens, so reading
every single-token key and every pair of the query's tokens finds it.

The prefix keeps these keys apart from ordinary terms, but it cannot make that certain: a
crawled page containing the text "widget:weather york" is stored under that very term. The
lookup must therefore keep only documents whose source is DocumentSource.WIDGET.
"""

from itertools import combinations

KEY_PREFIX = "widget:"

# 6 tokens give 6 single keys and 15 pairs: at most 21 page reads for a query.
MAX_QUERY_TOKENS = 6


def _key(tokens) -> str:
    return KEY_PREFIX + " ".join(sorted(tokens))


def storage_key(trigger_tokens: set[str], trigger_word: str, anchor: str) -> str:
    """The one key a widget document with these trigger tokens is stored under.

    `trigger_word` is the spec's word that puts it in the lookup's trigger set, `anchor` the
    instance's most distinctive token. Both must be trigger tokens of the document.
    """
    if trigger_word not in trigger_tokens or anchor not in trigger_tokens:
        raise ValueError(f"{trigger_word!r} and {anchor!r} must both be in {sorted(trigger_tokens)}")
    if len(trigger_tokens) == 1:
        return _key(trigger_tokens)
    if trigger_word == anchor:
        raise ValueError(f"The trigger word and the anchor must differ, both are {anchor!r}")
    return _key((trigger_word, anchor))


def query_keys(tokens: list[str]) -> list[str]:
    """Every key a document matching a query with these tokens could be stored under."""
    unique_tokens = list(dict.fromkeys(tokens))[:MAX_QUERY_TOKENS]
    single_keys = [_key([token]) for token in unique_tokens]
    pair_keys = [_key(pair) for pair in combinations(unique_tokens, 2)]
    return single_keys + pair_keys
