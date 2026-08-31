"""Deterministic checks over crawl evidence.

These carry most of the weight in a suggestion, and they are the reason a moderator can act
without having to trust a probability: "Homepage returns 404" is checkable, "0.83" is not.
Of the 76 distinct rejection details moderators have written, roughly a third are dead,
expired or squatted domains and another handful are "redirects to somewhere else" - all of
which are facts about a fetch rather than judgements about content.

Two kinds of rule, split by whether the answer can change while the domain does not:

* :func:`crawl_evidence` depends only on the crawl, so it is computed once by the enrichment
  task and stored on DomainEvidence.
* :func:`live_evidence` depends on the database - the submitter's record, and whether this
  domain has since been decided elsewhere - so it is evaluated per request. Both are indexed
  reads, and caching them would mean showing a moderator a stale "first submission from this
  user" after that user's tenth submission.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from logging import getLogger
from typing import Optional

logger = getLogger(__name__)

REJECT = "reject"
APPROVE = "approve"
NEUTRAL = "neutral"

# Domains we will not crawl whatever a submitter says, each of which has actually been
# submitted and rejected with a hand-written explanation ("don't crawl ourselves", "same as
# google.com no actual useful links apart from the search UI").
DO_NOT_CRAWL = frozenset({
    "mwmbl.org", "www.mwmbl.org",
    "google.com", "www.google.com", "scholar.google.com",
})

# Country TLDs that mostly carry non-English sites. This only ever produces a *neutral* "worth
# checking" line, never a rejection, because plenty of English sites live under .de and .nl -
# the reject-or-not call belongs to the model, whose TLD block learned .pt, .tw and .cn as
# reject-side features from actual decisions rather than from this list.
NON_ENGLISH_TLDS = frozenset({
    "cn", "tw", "jp", "kr", "ru", "ua", "pl", "cz", "sk", "hu", "ro", "bg", "gr", "tr",
    "pt", "es", "it", "fr", "de", "nl", "se", "no", "fi", "dk", "vn", "th", "id", "il", "ir",
})

# How much an earlier decision on the same domain is worth. Named because the queue's SQL
# reproduces this check to filter and order on it (mwmbl.moderation.suggest), and the two
# drifting apart would show a moderator one suggestion and filter on another.
PRIOR_DECISION_CONFIDENCE = 0.9
PRIOR_DECISION_REASON = "OTHER"


@dataclass(frozen=True)
class EvidenceItem:
    """One checkable fact, shown to the moderator as a line in the evidence list."""
    kind: str
    direction: str          # REJECT | APPROVE | NEUTRAL
    label: str              # moderator-facing, e.g. "Homepage returns 404"
    # Set when the fact is decisive on its own. The suggester takes the strongest of these
    # in preference to the model, because a 404 is not a matter of opinion.
    implies_action: Optional[str] = None
    implies_reason: str = ""
    implies_confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def crawl_evidence(domain: str, crawl: dict) -> list[EvidenceItem]:
    """Checks that depend only on the crawl. Stored on DomainEvidence by the enrichment task.

    ``crawl`` is the dict assembled by mwmbl.moderation.evidence.crawl_domain: http_status,
    final_domain, error, pages and signals.
    """
    items: list[EvidenceItem] = []
    pages = crawl.get("pages") or []
    signals = crawl.get("signals") or {}

    if domain in DO_NOT_CRAWL:
        items.append(EvidenceItem(
            "do_not_crawl", REJECT, "On the do-not-crawl list (we don't crawl ourselves, "
            "or it's another search engine)",
            implies_action="REJECT", implies_reason="OTHER", implies_confidence=0.99))

    error = crawl.get("error") or ""
    status = crawl.get("http_status")
    if error == "RobotsDenied":
        # Deliberately not decisive. It reads like a rejection but the record says otherwise:
        # lobste.rs disallows User-agent: * and was approved anyway, and not one of the 76
        # distinct rejection details moderators have written mentions robots.txt. The
        # crawler respects robots regardless of what is approved, so this is information a
        # moderator should have, not a decision to make for them.
        items.append(EvidenceItem(
            "robots", NEUTRAL,
            "robots.txt forbids our crawler - approving will not make this site crawlable"))
    elif error:
        items.append(EvidenceItem(
            "unreachable", REJECT, f"Could not be fetched ({error})",
            implies_action="REJECT", implies_reason="OTHER", implies_confidence=0.9))
    elif status is not None and not 200 <= status < 300:
        items.append(EvidenceItem(
            "http_status", REJECT, f"Homepage returns HTTP {status}",
            implies_action="REJECT", implies_reason="OTHER", implies_confidence=0.9))

    final_domain = crawl.get("final_domain") or ""
    if final_domain and registrable(final_domain) != registrable(domain):
        items.append(EvidenceItem(
            "redirect", REJECT, f"Redirects to {final_domain}",
            implies_action="REJECT", implies_reason="OTHER", implies_confidence=0.85))

    # Only meaningful when we actually got a page. After a failed or forbidden fetch there is
    # trivially no title and no text, and reporting three further "problems" would pad the
    # evidence list with restatements of the one fact that matters.
    fetched = bool(pages) and not error and status is not None and 200 <= status < 300
    if fetched and not any(page.get("title") for page in pages):
        items.append(EvidenceItem("no_title", REJECT, "No page on the site has a title"))
    if fetched and not any(page.get("extract") for page in pages):
        items.append(EvidenceItem(
            "no_extract", REJECT, "No readable body text found on any page"))
    if fetched and not signals.get("has_links"):
        items.append(EvidenceItem(
            "no_links", REJECT, "No links found to crawl on from the homepage"))

    # Language is left to the model rather than asserted here. crawl_url does not surface the
    # <html lang> attribute, and the one behavioural clue - justext finding no 'good'
    # paragraphs, which is why retrieve.py has an Open Graph fallback - fires just as readily
    # on a JavaScript-rendered English site. So: a hint for the moderator, and the model's TLD
    # block (which learned .pt, .tw and .cn as reject-side features) does the calling.
    if _tld(domain) in NON_ENGLISH_TLDS:
        items.append(EvidenceItem(
            "tld_language", NEUTRAL,
            f"Country domain (.{_tld(domain)}) - check the site is in English"))

    if signals.get("blacklisted"):
        # Strong evidence, but not decisive, and the reason is written down in
        # mwmbl.curated_domains: the remote lists are maintained for DNS ad-blocking and carry
        # false positives that are wrong for a search index - pudding.cool, contactmusic.com
        # and character.ai are all in The Block List Project's porn.txt. Overriding those is
        # precisely what an approval is *for*, so a decisive rejection here would have the
        # tool arguing against the mechanism it is supposed to be serving.
        items.append(EvidenceItem(
            "blocklist", REJECT,
            "On a public malware/adult blocklist - approving here also unblocks it. "
            "These lists are built for ad-blocking and do have false positives."))

    # Neutral for the same reason robots.txt is: a fact the moderator should have, not a
    # decision to make for them. Plenty of small personal sites - the ones this index exists
    # for - still have no certificate, and no rejection detail a moderator has written
    # mentions TLS. It only means anything when we actually reached the site: after a failed
    # fetch "https: False" says the https attempt failed, which the unreachable line already
    # said better.
    if fetched and signals.get("https") is False:
        items.append(EvidenceItem(
            "no_tls", NEUTRAL, "Served over plain HTTP - no TLS certificate"))

    if fetched:
        items.append(EvidenceItem(
            "reachable", APPROVE,
            f"Crawled {sum(1 for page in pages if page.get('title'))} of {len(pages)} "
            f"page(s) successfully"))

    return items


def live_evidence(submitter_record: dict, prior_decisions: dict) -> list[EvidenceItem]:
    """Checks whose answer changes without the domain changing, so never cached.

    ``submitter_record`` is {approved, rejected}; ``prior_decisions`` is {approved, rejected}
    for other submissions of the same domain.
    """
    items: list[EvidenceItem] = []

    approved = submitter_record.get("approved", 0)
    rejected = submitter_record.get("rejected", 0)
    total = approved + rejected
    if total == 0:
        # Worth saying plainly: on the last year of decisions, submissions from someone with no
        # track record were rejected 54% of the time, against 1% for established submitters.
        items.append(EvidenceItem(
            "submitter", NEUTRAL, "First submission from this user - no track record yet"))
    else:
        items.append(EvidenceItem(
            "submitter", APPROVE if rejected == 0 and approved >= 5 else NEUTRAL,
            f"Submitter has {approved} approved and {rejected} rejected"))

    if prior_decisions.get("approved"):
        items.append(EvidenceItem(
            "prior_decision", APPROVE, "This domain has already been approved before",
            implies_action="APPROVE", implies_confidence=PRIOR_DECISION_CONFIDENCE))
    elif prior_decisions.get("rejected"):
        items.append(EvidenceItem(
            "prior_decision", REJECT, "This domain has already been rejected before",
            implies_action="REJECT", implies_reason=PRIOR_DECISION_REASON,
            implies_confidence=PRIOR_DECISION_CONFIDENCE))

    return items


def decisive(items: list[EvidenceItem]) -> Optional[EvidenceItem]:
    """The strongest self-evident item, if any. Overrides the model when present."""
    decisive_items = [item for item in items if item.implies_action]
    if not decisive_items:
        return None
    return max(decisive_items, key=lambda item: item.implies_confidence)


# What a check tells the *submitter*, where its label is the wrong sentence to send. A label
# is written for the moderator deciding the case, so it may name the exception we caught or be
# written from our side of the transaction; a detail is read by the person whose site was
# rejected. Most labels serve both - "Homepage returns HTTP 404" is exactly what a submitter
# needs to hear, and naming the domain a site redirects to tells them what to submit instead -
# and the ones that do not are overridden here. Any new check that implies OTHER needs an
# entry unless its label reads as a sentence to the submitter.
#
# Keyed on kind rather than carried on the item so that evidence stored before an entry was
# written is covered too: crawl evidence is cached and a rescore reuses it, so a new field on
# EvidenceItem would stay empty until the domain happened to be crawled again.
SUBMITTER_DETAIL = {
    "unreachable": "We could not fetch this site when we tried to crawl it.",
    "do_not_crawl": "We don't index search engines or our own site.",
}

# "Some cached check implies OTHER", as a containment test the queue's SQL can run against the
# stored evidence. It is the same question :func:`other_detail` answers, and the two have to
# answer alike: the queue filters and orders thousands of rows on this without being able to
# call the Python (see mwmbl.moderation.suggest.annotate_queue), and where they disagree it
# hides rows that are on screen. Written down here so the pair stays visible from one place.
IMPLIES_OTHER = [{"implies_action": "REJECT", "implies_reason": "OTHER"}]


def implied_detail(item: EvidenceItem) -> str:
    """What a check's rejection tells the submitter, for a reason that says nothing on its own.

    Only OTHER needs one - "spam" explains itself, "other" does not - and for most checks the
    label is already that sentence; SUBMITTER_DETAIL covers the rest. Every OTHER a check
    implies has one of these, which is what lets the API refuse an OTHER rejection with no
    detail (mwmbl.platform.schemas.RejectionFieldsMixin) without making the suggestion
    untakeable.
    """
    if item.implies_reason != "OTHER":
        return ""
    return SUBMITTER_DETAIL.get(item.kind, item.label)


def other_detail(items: list[EvidenceItem]) -> str:
    """The sentence behind an OTHER rejection a check decided, or "" when none did.

    Deliberately not :func:`decisive`: the reason being explained is OTHER, so what explains it
    is the strongest check implying OTHER, which need not be the strongest check in the list.
    That is also the only form of the question SQL can ask of stored evidence - see
    IMPLIES_OTHER - which is what lets the queue agree with what the moderator is shown.
    """
    implying_other = [item for item in items
                      if item.implies_action == "REJECT" and item.implies_reason == "OTHER"]
    if not implying_other:
        return ""
    return implied_detail(max(implying_other, key=lambda item: item.implies_confidence))


def _tld(domain: str) -> str:
    labels = domain.lower().split(".")
    return labels[-1] if len(labels) > 1 else ""


def registrable(domain: str) -> str:
    """Last two labels, so www.example.com and example.com compare equal.

        >>> registrable("www.example.com"), registrable("docs.example.com")
        ('example.com', 'example.com')

    Deliberately naive about multi-part suffixes: every .co.uk host collapses to "co.uk", so
    two unrelated .co.uk sites compare equal and a redirect between them is *not* reported.

        >>> registrable("example.co.uk") == registrable("somewhere-else.co.uk")
        True

    That is a missed evidence line rather than a wrong one, which is the direction to be wrong
    in here: the redirect check is decisive, so over-reporting would mean confidently
    rejecting a site for moving between its own subdomains. A public-suffix list would fix it
    properly and is not worth a dependency for this.
    """
    labels = domain.lower().removeprefix("www.").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else domain.lower()
