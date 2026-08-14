"""Retroactively remove already-indexed documents whose domain is blacklisted.

Blacklisting (settings.EXCLUDED_DOMAINS / DOMAIN_BLACKLIST_REGEX / remote blocklist
providers) only stops *new* URLs being crawled/queued - it does nothing for pages
that were indexed before a domain was added to the blacklist. This command rewrites
index pages with blacklisted documents stripped out, in one of two modes:

- Targeted (--csv/--domain/--term, the normal way to run this): only touches the
  pages a document could plausibly be filed under. Seed terms come from tokenizing
  each query in a Search Console query-export CSV (the same tokenize+bigram terms
  real search retrieval would use), plus any --domain/--term given directly. Each
  document found on a seed page is checked against the full configured blacklist
  (so a --csv run auto-discovers which of the actual results are bad, no manual
  domain list needed) - and once flagged, its exact token set is recomputed
  (tokenize_document is a pure function of its stored url/title/extract, so this
  recovers every page it's really filed under, no guessing involved past the
  initial seed) and every one of those pages is purged too. Fast: touches
  dozens-hundreds of pages instead of the whole index. Not guaranteed exhaustive -
  it can only find documents reachable from the seed terms, so a page ranking only
  for some term no seed hits at all would be missed.

  Fast enough to run interactively - see the "Purge blacklisted domains" tool in
  the Django admin for a paste-queries-and-preview version of the same thing.

- Full scan (--full-scan): walks every page of the index against the full
  configured blacklist. Exhaustive but on the production index (~100M pages)
  expect this to take a long time - only use it for occasional completeness
  sweeps, never as the routine way to clean up a newly-found bad domain.

Run with --dry-run first to see how much would be removed.

Example, using a Search Console query export:

    manage.py purge_blacklisted_domains --csv Queries.csv --dry-run
"""
from logging import getLogger

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.indexer.purge_blacklisted import (
    guess_terms_for_domain, load_queries_from_csv, purge_pages, purge_targeted, seed_terms_for_query,
)
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

logger = getLogger(__name__)


class Command(BaseCommand):
    help = "Remove already-indexed documents belonging to blacklisted domains"

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            help="Path to a Search Console query-export CSV. Every result found for every "
                 "query in it is checked against the blacklist and purged if it matches.",
        )
        parser.add_argument(
            "--domain", action="append", default=[],
            help="Treat this domain as blacklisted for this run, regardless of the configured "
                 "blacklist, and seed the scan with terms guessed from it (repeatable)",
        )
        parser.add_argument(
            "--term", action="append", default=[],
            help="Extra seed query term, e.g. one you know matched from Search Console (repeatable)",
        )
        parser.add_argument(
            "--full-scan", action="store_true",
            help="Exhaustive scan of the whole index instead of a targeted one. SLOW - "
                 "~100M pages in production. Only for occasional completeness sweeps.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be removed without modifying the index",
        )
        parser.add_argument(
            "--progress-every", type=int, default=100_000,
            help="Log progress every N pages (--full-scan only)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        mode = "r" if dry_run else "w"
        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME

        csv_path = options["csv"]
        extra_domains = set(options["domain"])
        extra_terms = options["term"]

        if not options["full_scan"] and not (csv_path or extra_domains or extra_terms):
            raise ValueError("Provide --csv, --domain, --term, or --full-scan")

        with TinyIndex(item_factory=Document, index_path=index_path, mode=mode) as index:
            if options["full_scan"]:
                blacklist_provider = get_default_blacklist_provider()

                def progress(page_index, removed_by_domain):
                    if page_index and page_index % options["progress_every"] == 0:
                        logger.info(f"Scanned {page_index}/{index.num_pages} pages, "
                                    f"{sum(removed_by_domain.values())} documents removed so far")

                removed_by_domain, pages_changed, pages_scanned = purge_pages(
                    index, range(index.num_pages), blacklist_provider.is_domain_blacklisted, dry_run, progress)
            else:
                seed_terms = set(extra_terms)
                for domain in extra_domains:
                    seed_terms.update(guess_terms_for_domain(domain))
                if csv_path:
                    for query in load_queries_from_csv(csv_path):
                        seed_terms.update(seed_terms_for_query(query))

                blacklist_provider = get_default_blacklist_provider()
                is_blacklisted = lambda domain: domain in extra_domains or blacklist_provider.is_domain_blacklisted(domain)

                _, removed_by_domain, pages_changed, pages_scanned = purge_targeted(
                    index, seed_terms, is_blacklisted, dry_run)

        total_removed = sum(removed_by_domain.values())
        prefix = "[DRY RUN] Would remove" if dry_run else "Removed"
        self.stdout.write(f"{prefix} {total_removed} documents across {pages_changed} pages "
                           f"(scanned {pages_scanned} pages)")
        for domain, count in sorted(removed_by_domain.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {domain}: {count}")
