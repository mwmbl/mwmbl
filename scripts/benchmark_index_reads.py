"""Measure what dropping the index memory mapping buys, and what it costs.

Standard library only unless `--decode` is passed, so it can be copied to the server and
run with the system python against the production index - the point of the read-speed half
is to measure real disk and a real page cache, which a sparse file cannot model.

Two measurements, chosen separately:

    python3 scripts/benchmark_index_reads.py pagetables
    python3 scripts/benchmark_index_reads.py reads --index-path devdata/index-v2.tinysearch

`pagetables` builds a production-sized sparse file, touches one page in each 2 MiB window
both ways and reports VmPTE. That is the number the change exists for, and it needs no
index: expect roughly 800 MiB mapped against a few MiB with pread.

`reads` times random page reads both ways on a real index, single-threaded and across ten
threads. A warm mapped read is a memcpy, ~200 ns; a warm pread adds a syscall, ~1 us. A
query reads about ten pages, so ~10 us added per query against a zstd decompress and a JSON
parse of each of those pages.

Watch the threaded throughput rather than the threaded mean. A pread releases the GIL and
has to win it back, which a mapped read never does, so ten threads doing nothing but read
pages lose most of their throughput - far more than the syscall itself costs. Pass
`--decode` to time what get_page really does, the read plus the zstd decompress and the
JSON parse; that work dwarfs the handoff and is the number to judge the change on.

`--decode` also times the extension, which does the read, the decompress and the parse in
Rust with the GIL released. That is what buys the threaded throughput back: Python holds
the interpreter for the decompress and the parse, so its threads queue behind each other
whichever way they read the page.
"""

import argparse
import json
import mmap
import os
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

PAGE_SIZE = 4096
METADATA_SIZE = 4096

# One page table page covers 512 file pages, so touching one page per 2 MiB window is
# enough to allocate the whole table for that window - which is how a worker doing random
# lookups saturates its page tables long before it has read much of the index.
PAGE_TABLE_WINDOW = 2 * 1024 * 1024

# The production index: 102,400,000 pages of 4 KiB, 390 GiB.
PRODUCTION_NUM_PAGES = 102_400_000


def read_status_field(field: str) -> int:
    """The named /proc/self/status field in KiB."""
    with open("/proc/self/status") as status_file:
        for line in status_file:
            if line.startswith(field + ":"):
                return int(line.split()[1])
    raise ValueError(f"No {field} in /proc/self/status")


def report_memory(label: str):
    print(
        f"  {label:<12} VmPTE {read_status_field('VmPTE') / 1024:8.1f} MiB    VmRSS {read_status_field('VmRSS') / 1024:8.1f} MiB"
    )


def measure_page_tables(path: str, num_pages: int):
    """Touch one page per 2 MiB window through a mapping and through pread."""
    file_size = METADATA_SIZE + num_pages * PAGE_SIZE
    # Exclusive: this file is created, sized and then deleted, so it must be this run's own.
    # The flag is one letter from `reads --index-path`, and an index passed here by mistake
    # would be truncated to nothing and then removed.
    with open(path, "xb") as index_file:
        index_file.truncate(file_size)
    offsets = list(range(METADATA_SIZE, file_size - PAGE_SIZE, PAGE_TABLE_WINDOW))
    print(f"{file_size / 1024**3:.0f} GiB sparse file, {len(offsets)} windows touched")

    index_file = open(path, "r+b")
    report_memory("baseline")

    mapping = mmap.mmap(index_file.fileno(), 0, prot=mmap.PROT_READ)
    # Without this, a read fault maps 16 pages rather than one (fault-around), so the
    # resident set is 32 times the page tables and the kernel reclaims it on any machine
    # smaller than the server - and khugepaged then retracts the page tables the reclaim
    # emptied, which is exactly the number being measured. MADV_RANDOM is also what the
    # access pattern actually is: one page per lookup, nowhere near its neighbours.
    mapping.madvise(mmap.MADV_RANDOM)
    for offset in offsets:
        mapping[offset : offset + PAGE_SIZE]
    report_memory("mmap")
    mapping.close()
    report_memory("after close")

    fileno = index_file.fileno()
    for offset in offsets:
        os.pread(fileno, PAGE_SIZE, offset)
    report_memory("pread")
    index_file.close()
    os.remove(path)


def time_reads(read_page, page_indices: list[int]) -> list[float]:
    timings = []
    for page_index in page_indices:
        start = time.perf_counter()
        read_page(page_index)
        timings.append((time.perf_counter() - start) * 1e6)
    return timings


def report_timings(label: str, timings: list[float], elapsed: float):
    """Per-read latency, and the throughput of the run it came from.

    Both, because under threads they say different things. A pread releases the GIL and
    then has to win it back, so its per-read mean counts time the thread spent waiting for
    the interpreter rather than for the kernel. Throughput is what the worker actually
    gets.
    """
    ordered = sorted(timings)
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[int(len(ordered) * 0.99)]
    rate = len(timings) / elapsed / 1000
    print(
        f"  {label:<22} mean {statistics.mean(timings):8.2f} us   p50 {p50:8.2f} us   "
        f"p99 {p99:8.2f} us   {rate:7.1f}k reads/s"
    )


def decode_page(zstandard, page_data: bytes):
    """What reading a page in Python costs once the bytes are in hand.

    A fresh decompressor per page, as the Python version did - sharing one across threads
    corrupts its buffer.
    """
    return json.loads(zstandard.ZstdDecompressor().decompress(page_data).decode("utf8"))


def measure_reads(path: str, num_reads: int, num_threads: int, decode: bool):
    """Time random page reads on a real index, mapped and with pread."""
    file_size = os.path.getsize(path)
    num_pages = (file_size - METADATA_SIZE) // PAGE_SIZE
    generator = random.Random(2)
    page_indices = [generator.randrange(num_pages) for _ in range(num_reads)]
    print(f"{path}: {num_pages} pages, {num_reads} random reads, {num_threads} threads")

    index_file = open(path, "rb")
    fileno = index_file.fileno()
    mapping = mmap.mmap(fileno, 0, prot=mmap.PROT_READ)

    def read_mapped(page_index: int):
        offset = METADATA_SIZE + page_index * PAGE_SIZE
        return mapping[offset : offset + PAGE_SIZE]

    def read_pread(page_index: int):
        return os.pread(fileno, PAGE_SIZE, METADATA_SIZE + page_index * PAGE_SIZE)

    readers = [("mmap", read_mapped), ("pread", read_pread)]
    if decode:
        # Not at the top of the file: everything else here is standard library so the
        # script can run on the server with the system python, and only --decode needs
        # zstandard and the extension.
        import zstandard

        import mwmbl_rank

        def with_decode(read_page):
            return lambda page_index: decode_page(zstandard, read_page(page_index))

        def read_rust(page_index: int):
            return mwmbl_rank.read_index_page(fileno, METADATA_SIZE + page_index * PAGE_SIZE, PAGE_SIZE)

        # There is no raw mode for the extension: the read, the decompress and the parse
        # are one call with the GIL released, which is the whole point of it.
        readers = [(label, with_decode(read_page)) for label, read_page in readers] + [("rust", read_rust)]

    for label, read_page in readers:
        # Warm the page cache for this set of pages, so the single-threaded numbers are
        # about the read itself rather than about disk. Cold reads are dominated by the
        # disk either way; measure those on the server with the cache dropped.
        time_reads(read_page, page_indices)
        start = time.perf_counter()
        timings = time_reads(read_page, page_indices)
        report_timings(f"{label} single-threaded", timings, time.perf_counter() - start)

        chunk_size = num_reads // num_threads
        chunks = [page_indices[i : i + chunk_size] for i in range(0, num_reads, chunk_size)]
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            results = list(pool.map(lambda chunk: time_reads(read_page, chunk), chunks))
        elapsed = time.perf_counter() - start
        report_timings(f"{label} {num_threads} threads", [t for chunk in results for t in chunk], elapsed)

    mapping.close()
    index_file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="measurement", required=True)

    page_tables_parser = subparsers.add_parser("pagetables", help="page tables for a production-sized mapping")
    page_tables_parser.add_argument(
        "--sparse-file",
        default="/tmp/benchmark-index-sparse",
        help="the sparse file to create for the measurement, which must not exist and is deleted afterwards",
    )
    page_tables_parser.add_argument("--num-pages", type=int, default=PRODUCTION_NUM_PAGES)

    reads_parser = subparsers.add_parser("reads", help="read speed against a real index")
    reads_parser.add_argument("--index-path", required=True)
    reads_parser.add_argument("--num-reads", type=int, default=100_000)
    reads_parser.add_argument("--num-threads", type=int, default=10)
    reads_parser.add_argument(
        "--decode",
        action="store_true",
        help="time the whole of get_page - read, zstd decompress, JSON parse - which needs zstandard",
    )

    args = parser.parse_args()
    if args.measurement == "pagetables":
        measure_page_tables(args.sparse_file, args.num_pages)
    else:
        measure_reads(args.index_path, args.num_reads, args.num_threads, args.decode)


if __name__ == "__main__":
    main()
