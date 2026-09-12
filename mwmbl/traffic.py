"""What kind of client sent a search request.

Phase 0 of #410: before we can decide what an anonymous visitor is allowed to cost us, we
need to know how much of the traffic is people. This module answers only that question -
it classifies, and nothing acts on the answer. #412 turns a classification into a tier and
#413 is the first thing to gate on one.

Three classes, from the cheapest signals we have:

- DECLARED_BOT - the user agent matches the crawler-user-agents list, through the RegexSet
  in mwmbl_rank/src/user_agents.rs. The honest majority of bots say so, which is exactly
  the "requests we *know* are bots" criterion in #410.
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
from enum import StrEnum
from pathlib import Path

from django.http import HttpRequest

from mwmbl_rank import BotUserAgentMatcher

CRAWLER_USER_AGENTS_PATH = Path(__file__).parent / "resources" / "crawler_user_agents.json"

# nginx accepts an 8 KB header, and matching is linear in the length of what it is given, so
# this bounds a per-request cost the caller would otherwise choose. A real user agent is well
# under it.
MAX_USER_AGENT_LENGTH = 512

# A browser that sends nothing but this is not a browser. Every real one appends a platform
# and an engine.
BARE_MOZILLA_USER_AGENT = "Mozilla/5.0"


class ClientClass(StrEnum):
    """How plausible it is that a request came from a person using a browser.

    The values are written into Redis keys, so treat them as append-only.
    """

    DECLARED_BOT = "declared_bot"
    IMPLAUSIBLE = "implausible"
    BROWSER = "browser"


def _load_patterns() -> list[str]:
    with open(CRAWLER_USER_AGENTS_PATH) as patterns_file:
        return json.load(patterns_file)["patterns"]


# Compiled once at import into a single automaton with a literal prefilter, and shared from
# there. `'|'.join(...)` of the same patterns through Python's re measures 4.5 ms per
# *non-matching* user agent - and non-matching is the common case - because re backtracks
# through every branch. See mwmbl_rank/src/user_agents.rs.
_MATCHER = BotUserAgentMatcher(_load_patterns())


def is_declared_bot(user_agent: str) -> bool:
    """Whether the user agent says it is a crawler.

    Measured at 0.5 us a call, so there is no cache in front of it: there is nothing left
    for one to save, and a cache keyed on a string the caller chooses is a liability rather
    than a saving.
    """
    return _MATCHER.is_match(user_agent[:MAX_USER_AGENT_LENGTH])


def classify_client(request: HttpRequest) -> ClientClass:
    user_agent = request.headers.get("User-Agent", "")
    if is_declared_bot(user_agent):
        return ClientClass.DECLARED_BOT
    if not user_agent or user_agent == BARE_MOZILLA_USER_AGENT:
        return ClientClass.IMPLAUSIBLE
    if not request.headers.get("Accept-Language"):
        return ClientClass.IMPLAUSIBLE
    return ClientClass.BROWSER
