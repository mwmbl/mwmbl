"""Trace, step by step, exactly what the purge tool's discovery step sees.

Every component of the purge path has been verified working in isolation on production
(get_domain returns the right domain, is_domain_blacklisted returns True, and get_page()
demonstrably returns the documents - the raw search endpoint serves them), yet
discover_targeted_matches() returns nothing. This command runs the *same* code path the
admin view runs, printing each intermediate value, so the divergence can be observed
rather than guessed at.

It opens the index exactly the way admin_views._run_purge does (same path construction,
same mode) and is read-only.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.indexer.purge_blacklisted import (
    discover_targeted_matches, parse_pasted_queries, seed_terms_for_query,
)
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.utils import get_domain

MAX_DOCS_SHOWN = 40


class Command(BaseCommand):
    help = "Trace the purge tool's discovery step to find where it diverges from search"

    def add_arguments(self, parser):
        parser.add_argument("--query", required=True, help="The query to trace, e.g. fineartteens")

    def handle(self, *args, **options):
        raw_query = options["query"]

        # 1. Exactly what the admin view does with the pasted text.
        queries = parse_pasted_queries(raw_query)
        self.stdout.write(f"1. parse_pasted_queries({raw_query!r}) -> {queries}")

        seed_terms = set()
        for query in queries:
            terms = seed_terms_for_query(query)
            self.stdout.write(f"   seed_terms_for_query({query!r}) -> {sorted(terms)}")
            seed_terms.update(terms)
        self.stdout.write(f"2. seed_terms -> {sorted(seed_terms)}")

        # 2. Same index path construction as admin_views._run_purge.
        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME
        self.stdout.write(f"3. index_path -> {index_path!r}")

        blacklist_provider = get_default_blacklist_provider()
        self.stdout.write(f"4. blacklist provider -> {blacklist_provider.__class__.__name__} "
                           f"with {len(getattr(blacklist_provider, 'providers', []))} sub-providers")

        with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
            self.stdout.write(f"5. index opened: num_pages={index.num_pages} page_size={index.page_size}")

            for term in sorted(seed_terms):
                page_index = index.get_key_page_index(term)
                self.stdout.write(f"\n6. term {term!r} -> page {page_index}")

                documents = index.get_page(page_index)
                self.stdout.write(f"   index.get_page({page_index}) -> {len(documents)} documents")

                retrieved = index.retrieve(term)
                self.stdout.write(f"   index.retrieve({term!r}) -> {len(retrieved)} documents "
                                   f"(this is what search uses)")

                if not documents:
                    self.stdout.write(self.style.ERROR(
                        "   get_page() returned NOTHING here - but retrieve() uses the same call, "
                        "so if retrieve() found documents this is the divergence."))
                    continue

                self.stdout.write("   per-document verdicts (url -> domain -> blacklisted?):")
                blacklisted_count = 0
                for document in documents[:MAX_DOCS_SHOWN]:
                    try:
                        domain = get_domain(document.url)
                    except ValueError as e:
                        self.stdout.write(self.style.ERROR(
                            f"     {document.url!r} -> get_domain RAISED ValueError: {e} -> SKIPPED"))
                        continue
                    verdict = blacklist_provider.is_domain_blacklisted(domain)
                    if verdict:
                        blacklisted_count += 1
                    marker = "BLACKLISTED" if verdict else "kept"
                    self.stdout.write(f"     {document.url} -> {domain} -> {marker}")
                if len(documents) > MAX_DOCS_SHOWN:
                    self.stdout.write(f"     ... and {len(documents) - MAX_DOCS_SHOWN} more not shown")
                self.stdout.write(f"   -> {blacklisted_count} of the shown documents are blacklisted")

            # 3. Now the real function, unmodified.
            matches = discover_targeted_matches(
                index, seed_terms, blacklist_provider.is_domain_blacklisted)
            self.stdout.write(f"\n7. discover_targeted_matches(...) -> {len(matches)} matches")
            for match in matches[:MAX_DOCS_SHOWN]:
                self.stdout.write(f"     {match.domain}  {match.url}")

        if not matches:
            self.stdout.write(self.style.ERROR(
                "\ndiscover_targeted_matches returned NOTHING. Compare against the per-document "
                "verdicts above: if any document was marked BLACKLISTED there, the bug is inside "
                "discover_targeted_matches; if none were, the bug is upstream (wrong page, or "
                "get_page returning different data than retrieve)."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\ndiscover_targeted_matches found {len(matches)} matches - the discovery step is "
                "working in this process. If the admin page still shows none, the difference is in "
                "the web request context, not this logic."))
