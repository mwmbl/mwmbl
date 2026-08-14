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


def _run_purge(queries: list[str], dry_run: bool) -> PurgeResult:
    seed_terms = set()
    for query in queries:
        seed_terms.update(seed_terms_for_query(query))

    blacklist_provider = get_default_blacklist_provider()
    index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME
    mode = "r" if dry_run else "w"

    with TinyIndex(item_factory=Document, index_path=index_path, mode=mode) as index:
        matches, removed_by_domain, pages_changed, pages_scanned = purge_targeted(
            index, seed_terms, blacklist_provider.is_domain_blacklisted, dry_run)

    return PurgeResult(matches, removed_by_domain, pages_changed, pages_scanned, dry_run)


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

    return render(request, "admin/purge_blacklisted_domains.html", {
        "title": "Purge blacklisted domains",
        "queries_text": queries_text,
        "result": result,
        "error": error,
        "total_removed": sum(result.removed_by_domain.values()) if result else 0,
        "opts": {"app_label": "mwmbl"},
    })
