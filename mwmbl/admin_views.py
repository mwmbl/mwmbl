"""Custom (non-model) admin views."""
from dataclasses import dataclass

from django.conf import settings
from django.shortcuts import render

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.indexer.purge_blacklisted import MatchedDocument, parse_pasted_queries, purge_targeted, seed_terms_for_query
from mwmbl.tinysearchengine.indexer import Document, TinyIndex


@dataclass
class PurgeResult:
    matches: list[MatchedDocument]
    removed_by_domain: dict[str, int]
    pages_changed: int
    pages_scanned: int
    dry_run: bool
    # Recorded so a zero-match run can still explain itself: which queries were parsed out
    # of the pasted text, which terms they produced, which pages those hash to, and how many
    # documents were actually on those pages. Without this, "no matches" is indistinguishable
    # from "scanned nothing at all".
    queries: list[str]
    seed_terms: list[str]
    seed_pages: list[tuple[int, int]]  # (page index, document count on that page)


def _run_purge(queries: list[str], dry_run: bool) -> PurgeResult:
    seed_terms = set()
    for query in queries:
        seed_terms.update(seed_terms_for_query(query))

    blacklist_provider = get_default_blacklist_provider()
    index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME
    mode = "r" if dry_run else "w"

    with TinyIndex(item_factory=Document, index_path=index_path, mode=mode) as index:
        seed_pages = sorted({index.get_key_page_index(term) for term in seed_terms})
        seed_page_counts = [(page, len(index.get_page(page))) for page in seed_pages]

        matches, removed_by_domain, pages_changed, pages_scanned = purge_targeted(
            index, seed_terms, blacklist_provider.is_domain_blacklisted, dry_run)

    return PurgeResult(matches, removed_by_domain, pages_changed, pages_scanned, dry_run,
                       queries, sorted(seed_terms), seed_page_counts)


def purge_blacklisted_domains_view(request):
    """Registered via admin.site.admin_view() in admin.py, which already enforces
    staff-only access, CSRF, and cache-control - no decorator needed here."""
    queries_text = request.POST.get("queries", "")
    result = None
    error = None

    if request.method == "POST" and queries_text.strip():
        queries = parse_pasted_queries(queries_text)
        confirming = request.POST.get("confirm") == "1"

        if confirming and not request.user.is_superuser:
            error = "Only a superuser can confirm removal - you can still preview."
        else:
            result = _run_purge(queries, dry_run=not confirming)

    all_terms = []
    if result:
        seen = set()
        for match in result.matches:
            for term in match.found_via_terms + match.indexed_under_terms:
                if term not in seen:
                    seen.add(term)
                    all_terms.append(term)
        all_terms.sort()

    return render(request, "admin/purge_blacklisted_domains.html", {
        "title": "Purge blacklisted domains",
        "queries_text": queries_text,
        "result": result,
        "error": error,
        "total_removed": sum(result.removed_by_domain.values()) if result else 0,
        "all_terms": all_terms,
        "opts": {"app_label": "mwmbl"},
    })
