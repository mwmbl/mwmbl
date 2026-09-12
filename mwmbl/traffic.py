"""What kind of client sent a search request.

Phase 0 of #410: before we can decide what an anonymous visitor is allowed to cost us, we
need to know how much of the traffic is people. This module answers only that question -
it classifies, and nothing acts on the answer. #412 turns a classification into a tier and
#413 is the first thing to gate on one.

Three classes, from the cheapest signals we have:

- DECLARED_BOT - the user agent matches the crawler-user-agents list. The honest majority
  of bots say so, which is exactly the "requests we *know* are bots" criterion in #410.
- IMPLAUSIBLE - no user agent, a user agent of bare "Mozilla/5.0", or no Accept-Language.
  Deliberately no hand-curated list of HTTP client libraries: the crawler-user-agents list
  already matches curl, Wget, python-requests, httpx, Go-http-client, okhttp, axios,
  node-fetch, Apache-HttpClient, Python-urllib, aiohttp, libwww-perl and Scrapy.
- BROWSER - everything else.

The fourth class #411 names, "unattributed", is not here. It means "did not identify
itself as the sanctioned front end", and the front end cannot do that until
mwmbl/front-end#55 ships, so today the honest answer is that everything is unattributed
and there is nothing to count. #412 adds the shared-secret check along with the tier it
feeds.

Read what that means for the numbers before trusting them: the front end's server-side
render calls the backend with a bare fetch() and forwards no headers at all, so every
mwmbl.org search arrives with no browser user agent and lands in IMPLAUSIBLE. That is not
a misclassification to fix here - the size of that bucket on /api/v1/search/ is the
mwmbl.org search volume, which is the number this is all for.
"""

import json
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from django.http import HttpRequest

CRAWLER_USER_AGENTS_PATH = Path(__file__).parent / "resources" / "crawler_user_agents.json"

# nginx accepts an 8 KB header. Matching one against 1500 patterns, and worse keeping it in
# an unbounded cache, is a per-request cost anybody can choose for us; a real user agent is
# well under this.
MAX_USER_AGENT_LENGTH = 512

# A browser that sends nothing but this is not a browser. Every real one appends a platform
# and an engine.
BARE_MOZILLA_USER_AGENT = "Mozilla/5.0"

_REGEX_METACHARACTERS = frozenset(".^$*+?{}[]|()")


class ClientClass(StrEnum):
    """How plausible it is that a request came from a person using a browser.

    The values are written into Redis keys, so treat them as append-only.
    """

    DECLARED_BOT = "declared_bot"
    IMPLAUSIBLE = "implausible"
    BROWSER = "browser"


def _is_literal(pattern: str) -> bool:
    """Whether a crawler-user-agents pattern is a plain substring match.

    1464 of the 1500 are. A backslash before a non-alphanumeric escapes a literal character
    (`Googlebot\\/`); before an alphanumeric it starts a character class (`\\d`, `\\b`) and
    the pattern is a real regex.
    """
    position = 0
    while position < len(pattern):
        character = pattern[position]
        if character == "\\":
            if pattern[position + 1].isalnum():
                return False
            position += 2
            continue
        if character in _REGEX_METACHARACTERS:
            return False
        position += 1
    return True


def _build_trie(literals: list[str]) -> dict:
    trie: dict = {}
    for literal in literals:
        node = trie
        for character in literal:
            node = node.setdefault(character, {})
        node[""] = {}
    return trie


def _trie_pattern(trie: dict) -> str:
    """A regex matching everything in the trie, with common prefixes shared.

    This is the whole reason the matcher is usable on a request path. Python's re does not
    build a trie out of an alternation, so `'|'.join(1464 literals)` backtracks through
    every branch and measures 4.5 ms per *non-matching* user agent - and non-matching is
    the common case. Sharing the prefixes brings that to 44 us.
    """
    if len(trie) == 1 and "" in trie:
        return ""

    alternatives = []
    optional = False
    for character, subtrie in sorted(trie.items()):
        if character == "":
            optional = True
            continue
        alternatives.append(re.escape(character) + _trie_pattern(subtrie))

    body = alternatives[0] if len(alternatives) == 1 else "(?:" + "|".join(alternatives) + ")"
    return body + "?" if optional else body


def _compile_patterns(patterns: list[str]) -> tuple[re.Pattern, re.Pattern]:
    literals = sorted({re.sub(r"\\(.)", r"\1", pattern) for pattern in patterns if _is_literal(pattern)})
    regexes = [pattern for pattern in patterns if not _is_literal(pattern)]

    # Case-sensitive, both halves. Upstream encodes case deliberately - `[wW]get`,
    # `S[eE][mM]rushBot` - so folding it would both widen the match and slow it down.
    return re.compile(_trie_pattern(_build_trie(literals))), re.compile("|".join(regexes))


def _load_patterns() -> list[str]:
    with open(CRAWLER_USER_AGENTS_PATH) as patterns_file:
        return json.load(patterns_file)["patterns"]


_LITERAL_PATTERN, _REGEX_PATTERN = _compile_patterns(_load_patterns())


@lru_cache(maxsize=4096)
def is_declared_bot(user_agent: str) -> bool:
    """Whether the user agent says it is a crawler.

    Cached because the same few thousand user agents repeat all day, which takes the steady
    state to nothing and removes the amplification a per-request regex scan would otherwise
    offer. Bounded, so the cache cannot grow on attacker-chosen strings.
    """
    truncated = user_agent[:MAX_USER_AGENT_LENGTH]
    return _LITERAL_PATTERN.search(truncated) is not None or _REGEX_PATTERN.search(truncated) is not None


def classify_client(request: HttpRequest) -> ClientClass:
    user_agent = request.headers.get("User-Agent", "")
    if is_declared_bot(user_agent):
        return ClientClass.DECLARED_BOT
    if not user_agent or user_agent == BARE_MOZILLA_USER_AGENT:
        return ClientClass.IMPLAUSIBLE
    if not request.headers.get("Accept-Language"):
        return ClientClass.IMPLAUSIBLE
    return ClientClass.BROWSER
