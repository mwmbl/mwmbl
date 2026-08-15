"""Read-only, safety-bounded diagnostic for a single TinyIndex page.

Investigating reports of a purge tool finding "no results" for a term while live search
returns many results for the same term, AND a worker OOM-crashing on that exact query.
One theory: TinyIndex has no locking between writers (background indexer, POST /results,
this purge tool, etc. can all open the index in write mode and write concurrently), and
mmap slice writes aren't guaranteed atomic against a concurrent reader - a write racing a
read could produce a torn zstd frame. _get_page_tuples() decompresses with no output-size
cap, trusting the frame's embedded declared size; a corrupted/torn frame could either fail
to decompress (caught and silently returned as [] - explaining "no results") or have a
garbage declared size large enough that decompression tries to allocate an enormous buffer
(explaining the OOM).

This command reads the raw bytes for one page directly and attempts a *bounded*
decompression (max_output_size capped well below anything a legitimate page could produce,
since page_size is 4096 bytes compressed), so it can't reproduce the OOM while diagnosing it.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand
from zstandard import ZstdDecompressor, ZstdError

from mwmbl.tinysearchengine.indexer import Document, METADATA_SIZE, TinyIndex
from mwmbl.utils import get_domain

# A legitimate page is at most page_size (4096) bytes *compressed*. Even a pathological
# compression ratio shouldn't get anywhere near this decompressed - it's just a safety net
# against a corrupted/garbage declared size in a torn frame.
MAX_OUTPUT_SIZE = 200_000_000


class Command(BaseCommand):
    help = "Inspect a single TinyIndex page's raw bytes without risking the OOM this is investigating"

    def add_arguments(self, parser):
        parser.add_argument("--term", help="Report on the page this term hashes to")
        parser.add_argument("--page", type=int, help="Report on this page index directly")

    def handle(self, *args, **options):
        term = options["term"]
        page_index = options["page"]
        if term is None and page_index is None:
            raise ValueError("Provide --term or --page")

        index_path = settings.DATA_PATH + "/" + settings.INDEX_NAME

        with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
            if page_index is None:
                page_index = index.get_key_page_index(term)
                self.stdout.write(f"Term {term!r} hashes to page {page_index} (num_pages={index.num_pages})")
            elif term is not None:
                self.stdout.write(f"Also checking: term {term!r} hashes to page "
                                   f"{index.get_key_page_index(term)} (requested page {page_index} directly)")

            page_size = index.page_size
            raw = bytes(index.mmap[page_index * page_size + METADATA_SIZE:(page_index + 1) * page_size + METADATA_SIZE])

        self.stdout.write(f"Raw page bytes: {len(raw)} (page_size={page_size})")

        # A well-formed page is a zstd frame followed by NUL padding out to page_size.
        # Try to find where the trailing NUL run starts, from the end.
        trailing_nul = 0
        for b in reversed(raw):
            if b != 0:
                break
            trailing_nul += 1
        self.stdout.write(f"Trailing NUL bytes: {trailing_nul} "
                           f"({'looks like normal padding' if trailing_nul > 0 else 'NO trailing padding - unusual'})")

        decompressor = ZstdDecompressor()
        try:
            decompressed = decompressor.decompress(raw, max_output_size=MAX_OUTPUT_SIZE)
        except ZstdError as e:
            self.stdout.write(self.style.ERROR(f"DECOMPRESSION FAILED: {e}"))
            self.stdout.write(self.style.ERROR(
                "This is consistent with a corrupted/torn frame. get_page() catches exactly "
                "this exception and silently returns [] - which would explain \"no results\" "
                "from the purge tool for this page."))
            return

        self.stdout.write(f"Decompressed size: {len(decompressed)} bytes")
        if len(decompressed) > 5_000_000:
            self.stdout.write(self.style.WARNING(
                "Decompressed size is much larger than a normal page - if this were "
                "uncapped (as the real get_page() call is), this could plausibly OOM a worker."))

        try:
            items = json.loads(decompressed.decode("utf8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.stdout.write(self.style.ERROR(f"JSON PARSE FAILED after successful decompression: {e}"))
            self.stdout.write(self.style.ERROR(
                "Decompression succeeded but the bytes aren't valid JSON - also consistent with "
                "a torn write mixing bytes from two different writes."))
            return

        self.stdout.write(f"Item count: {len(items)}")
        domains = {}
        for item in items:
            url = item[1] if len(item) > 1 else "?"
            try:
                domain = get_domain(url)
            except (ValueError, IndexError):
                domain = "?"
            domains[domain] = domains.get(domain, 0) + 1

        self.stdout.write("Domains on this page:")
        for domain, count in sorted(domains.items(), key=lambda kv: -kv[1])[:20]:
            self.stdout.write(f"  {domain}: {count}")
