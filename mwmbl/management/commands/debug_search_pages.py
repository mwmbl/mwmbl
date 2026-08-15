"""Safely inspect every index page a query touches, without ever decompressing a bad one.

A query family (e.g. "fineart", "fineartteens") can hang the search endpoint and get the
worker OOM-killed, while the same query's /raw endpoint returns instantly and other queries
are fine. /raw only reads the page for the query's own term; full search additionally
retrieves the completer's completions and the query bigrams, so it touches pages /raw never
does. If one of those pages holds a zstd frame whose *declared* decompressed size is
garbage - which a torn/partial write can produce, since TinyIndex writes pages via unlocked
mmap slice assignment with no reader/writer coordination - then _get_page_tuples()'s
unbounded decompressor.decompress(page_data) will try to allocate that declared size and
take the process down.

This command mirrors Ranker.get_results()'s retrieval-term computation, then for each page
reads only the zstd *frame header* via get_frame_parameters(), which reports the declared
content size without allocating anything. Only pages whose declared size is sane are then
actually decompressed (and even then, with an explicit cap). So this can identify the
offending page without reproducing the crash.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand
from zstandard import ZstdDecompressor, ZstdError, get_frame_parameters

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, METADATA_SIZE, TinyIndex
from mwmbl.tokenizer import get_bigrams, tokenize
from mwmbl.utils import get_domain

# A page is at most page_size (4096) bytes compressed. Anything claiming to decompress to
# more than this is not a page this code ever legitimately wrote.
SANE_DECOMPRESSED_SIZE = 10_000_000


class Command(BaseCommand):
    help = "Inspect every index page a query touches, without decompressing a corrupted one"

    def add_arguments(self, parser):
        parser.add_argument("--query", required=True)

    def handle(self, *args, **options):
        query = options["query"]

        terms = tokenize(query)
        is_complete = query.endswith(' ')
        completer = Completer()
        completions = completer.complete(terms[-1]) if terms and not is_complete else []
        retrieval_terms = set(terms + completions) if completions or terms else set()
        bigrams = set(get_bigrams(len(terms), terms))
        curation_term = " ".join(terms)

        self.stdout.write(f"query           : {query!r}")
        self.stdout.write(f"tokens          : {terms}")
        self.stdout.write(f"completions     : {completions}   <- full search retrieves these too; /raw does not")
        self.stdout.write(f"bigrams         : {sorted(bigrams)}")
        self.stdout.write(f"curation term   : {curation_term!r}")

        all_terms = sorted((retrieval_terms | bigrams) | {curation_term})
        self.stdout.write(f"\nterms retrieved by full search: {all_terms}\n")

        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME
        suspect_pages = []

        with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
            seen_pages = {}
            for term in all_terms:
                page_index = index.get_key_page_index(term)
                seen_pages.setdefault(page_index, []).append(term)

            for page_index, terms_for_page in sorted(seen_pages.items()):
                page_size = index.page_size
                start = page_index * page_size + METADATA_SIZE
                raw = bytes(index.mmap[start:start + page_size])

                label = f"page {page_index} (terms: {', '.join(repr(t) for t in terms_for_page)})"

                # Header only - no allocation, safe even if the frame is garbage.
                try:
                    params = get_frame_parameters(raw)
                except ZstdError as e:
                    self.stdout.write(self.style.ERROR(
                        f"{label}: NOT A VALID ZSTD FRAME: {e}"))
                    suspect_pages.append(page_index)
                    continue

                declared = params.content_size
                if declared is None or declared > SANE_DECOMPRESSED_SIZE:
                    self.stdout.write(self.style.ERROR(
                        f"{label}: declared decompressed size = {declared!r} "
                        f"- PATHOLOGICAL (a 4096-byte page cannot legitimately claim this). "
                        f"The real get_page() would try to allocate this and take the worker down."))
                    suspect_pages.append(page_index)
                    continue

                try:
                    decompressed = ZstdDecompressor().decompress(
                        raw, max_output_size=SANE_DECOMPRESSED_SIZE)
                    items = json.loads(decompressed.decode("utf8"))
                except (ZstdError, json.JSONDecodeError, UnicodeDecodeError) as e:
                    self.stdout.write(self.style.ERROR(f"{label}: FAILED to read: {e}"))
                    suspect_pages.append(page_index)
                    continue

                domains = {}
                for item in items:
                    try:
                        domains[get_domain(item[1])] = domains.get(get_domain(item[1]), 0) + 1
                    except (ValueError, IndexError):
                        domains["?"] = domains.get("?", 0) + 1
                top = ", ".join(f"{d}({c})" for d, c in
                                sorted(domains.items(), key=lambda kv: -kv[1])[:5])
                self.stdout.write(f"{label}: OK - declared {declared} bytes, "
                                   f"{len(items)} items. Top domains: {top}")

        self.stdout.write("")
        if suspect_pages:
            self.stdout.write(self.style.ERROR(
                f"SUSPECT PAGES: {suspect_pages}\n"
                f"These are read by full search but not by /raw, which is why /raw works while "
                f"search hangs. Rewriting a suspect page with valid content (or empty) should "
                f"restore the query."))
        else:
            self.stdout.write(self.style.SUCCESS(
                "All pages this query touches are well-formed - the hang is not a corrupted page."))
