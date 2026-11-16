"""The widget tokenizer: a query as the set of tokens widgets are triggered by.

Values are replaced by placeholders, so one widget serves every value: "10km in miles"
and "3 km to miles" are both {:n, km, miles}.
"""

import re

NUMBER = ":n"
MONEY = ":money"
DATE = ":date"
TIME = ":time"

# Searches are short; cutting here keeps a pathological query from costing the regexes more.
MAX_QUERY_CHARS = 200

STOPWORDS = frozenset("a an the in of to for is are what whats how me my please about at on and or into".split())

# Placeholders are swapped in as private-use characters, so that a ":date" typed in the query
# can never come out as the placeholder. They are cut from the input first.
_SENTINELS = {"": DATE, "": TIME, "": MONEY}
_SENTINEL_CHARS = "".join(_SENTINELS)
_SENTINEL_STRIPPER = re.compile(f"[{_SENTINEL_CHARS}]")

_DATE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?\b|\b\d{1,2}\s?[ap]m\b")
_AMOUNT = r"\d[\d,]*(?:\.\d+)?"
_MONEY = re.compile(rf"[$£€¥]\s?{_AMOUNT}|\b{_AMOUNT}\s?[$£€¥]")
# A number is split off the word it is written against ("10km"); anything else made of
# letters and digits stays whole ("co2").
_TOKEN = re.compile(rf"[{_SENTINEL_CHARS}]|\d[\d,.]*\d|\d|%|[^\W_]+")


def widget_tokens(text: str) -> list[str]:
    """The tokens of a query for widget lookup, without repeats, in order of first appearance.

    Lowercased, stopwords dropped, `10km` split into `:n km`, values replaced by `:n`,
    `:money`, `:date` and `:time`. `%` is a token of its own.
    """
    lowered = _SENTINEL_STRIPPER.sub("", text[:MAX_QUERY_CHARS].lower())
    without_apostrophes = lowered.replace("'", "").replace("’", "")
    marked = _DATE.sub("", without_apostrophes)
    marked = _TIME.sub("", marked)
    marked = _MONEY.sub("", marked)

    tokens = []
    for match in _TOKEN.finditer(marked):
        raw = match.group()
        if raw in _SENTINELS:
            token = _SENTINELS[raw]
        elif raw[0].isdigit():
            token = NUMBER
        else:
            token = raw
        if token in STOPWORDS or token in tokens:
            continue
        tokens.append(token)
    return tokens
