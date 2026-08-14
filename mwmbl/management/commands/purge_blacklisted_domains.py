"""Retroactively remove already-indexed documents whose domain is blacklisted.

Blacklisting (settings.EXCLUDED_DOMAINS / DOMAIN_BLACKLIST_REGEX / remote blocklist
providers) only stops *new* URLs being crawled/queued - it does nothing for pages
that were indexed before a domain was added to the blacklist. This command rewrites
index pages with blacklisted documents stripped out, in one of two modes:

- Targeted (--domain, recommended for a handful of known-bad domains): only touches
  the pages a document could plausibly be filed under, found by combining known query
  terms with terms guessed from the domain itself, then exactly recomputing each
  matched document's full token set (tokenize_document is a pure function of its
  stored url/title/extract, so this recovers every page it's really filed under - no
  guessing involved past the initial seed). Fast: touches dozens-hundreds of pages
  instead of the whole index. Not guaranteed exhaustive - it can only find documents
  reachable from the seed terms, so a page ranking only for terms no seed hits at all
  would be missed.

- Full scan (no --domain, default): walks every page of the index against the full
  configured blacklist (all providers, including the ~950k-domain remote list).
  Exhaustive but on the production index (~100M pages) expect this to take a long
  time - use this as an occasional completeness sweep, not the everyday tool.

Run with --dry-run first to see how much would be removed.
"""
from logging import getLogger

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.indexer.index import get_index_tokens, prepare_url_for_tokenizing, tokenize_document
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tokenizer import tokenize
from mwmbl.utils import get_domain

logger = getLogger(__name__)


def _split_page(documents, is_blacklisted):
    """Partition a page's documents into (kept, removed) by domain."""
    kept, removed = [], []
    for document in documents:
        try:
            domain = get_domain(document.url)
        except ValueError:
            kept.append(document)
            continue
        (removed if is_blacklisted(domain) else kept).append(document)
    return kept, removed


def _guess_terms_for_domain(domain: str) -> set[str]:
    """Terms the indexer would have derived from this domain appearing in a URL."""
    url_tokens = tokenize(prepare_url_for_tokenizing(f"https://{domain}/"))
    return get_index_tokens(url_tokens)


class Command(BaseCommand):
    help = "Remove already-indexed documents belonging to blacklisted domains"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be removed without modifying the index",
        )
        parser.add_argument(
            "--domain", action="append", default=[],
            help="Run a fast targeted purge for this domain instead of a full scan (repeatable)",
        )
        parser.add_argument(
            "--term", action="append", default=[],
            help="Extra known query term to seed the targeted scan with, e.g. from Search "
                 "Console (repeatable, only used together with --domain)",
        )
        parser.add_argument(
            "--progress-every", type=int, default=100_000,
            help="Log progress every N pages (full scan only)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        domains = options["domain"]
        mode = "r" if dry_run else "w"
        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME

        if options["term"] and not domains:
            raise ValueError("--term only makes sense together with --domain")

        with TinyIndex(item_factory=Document, index_path=index_path, mode=mode) as index:
            if domains:
                removed_by_domain, pages_changed, pages_scanned = self._purge_targeted(
                    index, set(domains), options["term"], dry_run)
            else:
                removed_by_domain, pages_changed, pages_scanned = self._purge_full_scan(
                    index, dry_run, options["progress_every"])

        total_removed = sum(removed_by_domain.values())
        prefix = "[DRY RUN] Would remove" if dry_run else "Removed"
        self.stdout.write(f"{prefix} {total_removed} documents across {pages_changed} pages "
                           f"(scanned {pages_scanned} pages)")
        for domain, count in sorted(removed_by_domain.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {domain}: {count}")

    def _purge_targeted(self, index: TinyIndex, domain_set: set[str], extra_terms: list[str], dry_run: bool):
        seed_terms = set(extra_terms)
        for domain in domain_set:
            seed_terms.update(_guess_terms_for_domain(domain))
        seed_pages = {index.get_key_page_index(term) for term in seed_terms}

        # Find every document on a seed page that belongs to a target domain, then
        # recompute its exact token set to discover every other page it's filed under.
        candidate_pages = set(seed_pages)
        seen_urls = set()
        for page_index in seed_pages:
            for document in index.get_page(page_index):
                try:
                    domain = get_domain(document.url)
                except ValueError:
                    continue
                if domain in domain_set and document.url not in seen_urls:
                    seen_urls.add(document.url)
                    tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
                    candidate_pages.update(index.get_key_page_index(token) for token in tokenized.tokens)

        return self._purge_pages(index, candidate_pages, lambda domain: domain in domain_set, dry_run)

    def _purge_full_scan(self, index: TinyIndex, dry_run: bool, progress_every: int):
        blacklist_provider = get_default_blacklist_provider()

        def progress(page_index, removed_by_domain):
            if page_index and page_index % progress_every == 0:
                logger.info(f"Scanned {page_index}/{index.num_pages} pages, "
                            f"{sum(removed_by_domain.values())} documents removed so far")

        return self._purge_pages(
            index, range(index.num_pages), blacklist_provider.is_domain_blacklisted, dry_run, progress)

    def _purge_pages(self, index: TinyIndex, page_indexes, is_blacklisted, dry_run, on_progress=None):
        removed_by_domain = {}
        pages_changed = 0
        pages_scanned = 0

        for page_index in page_indexes:
            pages_scanned += 1
            kept, removed = _split_page(index.get_page(page_index), is_blacklisted)

            if removed:
                pages_changed += 1
                for document in removed:
                    domain = get_domain(document.url)
                    removed_by_domain[domain] = removed_by_domain.get(domain, 0) + 1
                if not dry_run:
                    index.store_in_page(page_index, kept)

            if on_progress:
                on_progress(page_index, removed_by_domain)

        return removed_by_domain, pages_changed, pages_scanned
