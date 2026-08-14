"""Retroactively remove already-indexed documents whose domain is blacklisted.

Blacklisting (settings.EXCLUDED_DOMAINS / DOMAIN_BLACKLIST_REGEX / remote blocklist
providers) only stops *new* URLs being crawled/queued - it does nothing for pages
that were indexed before a domain was added to the blacklist. This command walks
every page of the TinyIndex and rewrites it with blacklisted documents stripped out.

This is a full index scan: on the production index (~100M pages) expect this to
take a long time. Run with --dry-run first to see how much would be removed.
"""
from logging import getLogger

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.utils import get_domain

logger = getLogger(__name__)


class Command(BaseCommand):
    help = "Remove already-indexed documents belonging to blacklisted domains"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be removed without modifying the index",
        )
        parser.add_argument(
            "--progress-every", type=int, default=100_000,
            help="Log progress every N pages",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        progress_every = options["progress_every"]

        blacklist_provider = get_default_blacklist_provider()
        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME

        mode = "r" if dry_run else "w"
        removed_by_domain = {}
        pages_changed = 0

        with TinyIndex(item_factory=Document, index_path=index_path, mode=mode) as index:
            for page_index in range(index.num_pages):
                documents = index.get_page(page_index)
                if not documents:
                    continue

                kept, removed = [], []
                for document in documents:
                    try:
                        domain = get_domain(document.url)
                    except ValueError:
                        kept.append(document)
                        continue
                    if blacklist_provider.is_domain_blacklisted(domain):
                        removed.append(document)
                    else:
                        kept.append(document)

                if removed:
                    pages_changed += 1
                    for document in removed:
                        domain = get_domain(document.url)
                        removed_by_domain[domain] = removed_by_domain.get(domain, 0) + 1
                    if not dry_run:
                        index.store_in_page(page_index, kept)

                if page_index and page_index % progress_every == 0:
                    logger.info(
                        f"Scanned {page_index}/{index.num_pages} pages, "
                        f"{sum(removed_by_domain.values())} documents removed so far"
                    )

        total_removed = sum(removed_by_domain.values())
        prefix = "[DRY RUN] Would remove" if dry_run else "Removed"
        self.stdout.write(f"{prefix} {total_removed} documents across {pages_changed} pages")
        for domain, count in sorted(removed_by_domain.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {domain}: {count}")
