# Replace the index memory mapping with positioned reads and writes

Part of #374.

## Context

Production workers are being OOM-killed. The kernel log on the host shows every kill is
`global_oom` with `constraint=CONSTRAINT_NONE`: no container limit is involved, the whole
31 GiB machine runs out, and the oom killer picks a mwmbl worker because those processes
score highest. Redis, Postgres, nginx and docker-buildx are the ones invoking the killer.

The dominant consumer is page tables, not resident memory. Each kill line reports:

| Per killed worker | |
|---|---|
| Virtual size | 430 GB |
| Anonymous resident | 480 to 850 MB |
| Page tables | 802 MiB |

`TinyIndex` maps the whole index. At 102,400,000 pages of 4 KiB that is 390 GiB of
mapping, whose fully populated page tables come to 781 MiB, plus 29 MiB for the 15 GB
external cache. Theoretical ceiling 810 MiB, observed 802 MiB, so these workers hold page
table entries for essentially the entire index.

It saturates quickly because one 4 KiB page table page covers 512 file pages, or 2 MiB.
Touching a single index page anywhere in a 2 MiB window allocates the whole table for that
window, and there are only about 200,000 windows. Random lookups fill the tables long
before the worker has read any meaningful fraction of the file. That memory is kernel
memory: it cannot be swapped, cannot be reclaimed under pressure, and is freed only when
the mapping goes away.

Two containers run the search app, `api.mwmbl.org` and `beta.mwmbl.org`, seventeen workers
each. At saturation that is roughly 27 GiB of unreclaimable page tables on a 31 GiB
machine, before a single search result.

The mapping buys nothing here. `_get_page_tuples` slices the mapping, which allocates a new
bytes object and copies 4 KiB into it, and `_write_page` copies a 4 KiB buffer in the other
direction. Both are byte for byte equivalent to a positioned read or write on the same file
descriptor. We are paying for the mapping and taking the copy anyway.

**Intended outcome:** page tables per worker drop from ~800 MiB to a few MiB, with no
measurable change to search latency.

## Scope

This pull request changes index reads and writes only. Deliberately not included, each
worth its own issue:

- The worker count, derived from the host's eight cores as `cpu_count() * 2 + 1`.
- Roughly 115 MB per worker of pandas, scipy and scikit-learn, imported into every web
  worker because app startup reaches `mwmbl.background` to schedule tasks.
- No memory limit on the app containers, so a runaway can take out Postgres.
- Dokku building images on the same host it serves from.

Say so in the pull request. Even at zero page tables, 34 workers at ~550 MB anonymous each
is about 18 GiB on a 31 GiB box, so expect a large improvement rather than immunity.

## The change

All of it is in `mwmbl/tinysearchengine/indexer.py`. Nothing outside that file references
`.mmap`, and the one external use of `.index_file` is a test that only needs `fileno()`.

**`__enter__`** — keep `self.index_file = open(self.index_path, "r+b")` and drop the
`mmap` call and the `PROT_READ`/`PROT_WRITE` selection. Keep the buffered file object
rather than a raw descriptor: `fileno()` already feeds the OFD locks in `_set_page_lock`
and `test_index_page_writes.py`, and nothing ever reads or writes through the buffer, so
there is no coherence hazard. `__exit__` closes the file only. Drop `self.mmap` and the
`mmap`/`PROT_READ`/`PROT_WRITE` import.

**`_get_page_tuples`** (line 420) — replace the slice with
`os.pread(self.index_file.fileno(), self.page_size, i * self.page_size + METADATA_SIZE)`.
A short read means a truncated or corrupt file, so raise `PageError` when the returned
length is not `page_size`, in keeping with the house rule against papering over an
unexpected shape. Callers already handle `PageError`.

**`_write_page`** (line 452) — replace the slice assignment with `os.pwrite` at the same
offset, keeping the existing `mode != "w"` guard. Raise if the count written is short.

**Comments** — several argue the torn-read window from "a single memcpy" and need
rewording to the equivalent write. They are load-bearing documentation of why writers hold
OFD locks and readers do not, so update rather than delete: `_locked_page` (329-338),
`_get_page_tuples` (411-419), `page` (455-479), `_OpenPage` (247-263), and the module
docstring of `test/test_index_page_writes.py`. The semantics do not change. A buffered
write is not atomic against a concurrent read any more than the memcpy was, so the window
and the lock that guards it stay exactly as they are.

`TinyIndex.create` needs no change: it already writes the file sequentially through a plain
handle (lines 495-498).

### Why the cross-process behaviour is preserved

Two places depend on a long-lived read handle seeing writes made elsewhere with no flush
and no reopen: the per-worker search handle at `mwmbl/search_setup.py:34` and the external
cache handle at `mwmbl/indexer/external_cache.py:89`. Writers open short-lived `"w"`
handles, including from the request path in `store_external_results`.

Positioned reads and writes go through the same page cache that backs a shared mapping, so
a reader sees a writer's bytes as soon as the write returns, exactly as now. This also
means a mixed deployment is safe: during the rollout an old container still mapping the
file and a new one writing with `pwrite` stay coherent, because on Linux shared file
mappings and `read`/`write` are the same pages.

## Verification

### Correctness

`make test` with `DATABASE_URL="postgres://daoud@"`, and `make check`. The existing suite is
a real safety net for this change and should pass untouched:

- `test/test_index_page_writes.py` — OFD lock exclusion across a forked child using
  `index_file.fileno()`, per-page lock granularity, lock-degradation paths, four concurrent
  writers on one page, and five corruption tests that damage a live page through a second
  plain file handle and read it back through a `TinyIndex`. Those last ones exercise
  precisely the cross-handle visibility this change relies on.
- `test/test_external_cache.py` — about forty tests over the long-lived read handle against
  short-lived write handles, including store then retrieve without reopening.
- `test/test_indexer.py`, `test/test_index_batches.py`, `test/test_purge_blacklisted.py`,
  `test/test_copy_index.py` — round trips, full-index scans, and a copy out of the real
  `devdata/index-v2.tinysearch`.

Add two tests to `test/test_indexer.py`:

1. A write through a separate `"w"` handle is visible to an already-open `"r"` handle with
   no reopen. The property search depends on, and currently only asserted for the external
   cache.
2. Truncating the file mid-page makes the read raise `PageError` rather than returning a
   short page.

### Memory saving

New `scripts/benchmark_index_reads.py`, standard library only so it can be copied to the
server and run with the system python against the production index. Two measurements,
selectable so the cheap one can run anywhere:

**Page tables.** Build a production-sized sparse file, `truncate` to `NUM_PAGES *
PAGE_SIZE`, which consumes no disk. Touch one page in each 2 MiB window both ways and
report `VmPTE` and `VmRSS` from `/proc/self/status`. Expect roughly 800 MiB against a few
MiB. This is the number the whole change exists for, and it can be demonstrated before
touching production.

**In production, after deploy.** `grep VmPTE /proc/*/status` inside the container, summed
across workers, against the ~800 MiB per worker baseline. Then `free -h` on the host for
the headline, and the kernel log for the absence of further kills:

```sh
journalctl -k --since '<deploy time>' | grep -i oom
```

### Read speed

The same script, on a real index, timing random `get_page` calls both ways and reporting
mean, p50 and p99 per page, single-threaded and across ten threads. Base it on
`analyse/index_concurrency.py`, which already does ten threads issuing a million random
`get_page` calls but prints no timings.

What to expect, and what would count as a regression. A warm mapping read is a memcpy of
4 KiB, roughly 200 ns. A warm positioned read adds a system call, roughly 1 µs. A query
issues about ten page reads: `get_results` in `mwmbl/tinysearchengine/rank.py:335` reads one
curation page plus one per term, completion and bigram. So the arithmetic says about 10 µs
added per query, against a zstd decompress and JSON parse of every one of those pages.
Treat a measured end-to-end delta larger than a few hundred microseconds per query as a
reason to stop and look again.

Cold reads are dominated by disk either way and cannot be modelled from a sparse file, so
measure those on the server against the real index.

Two existing paths are worth a second look because they scan the whole index rather than a
handful of pages: `copy_pages` in `mwmbl/tinysearchengine/copy_index.py`, and the daily 1%
sample in `mwmbl/count_urls.py:45-72`. Both are already dominated by per-page decompression,
but `count_urls` is the natural place to notice a throughput change in production.

## Risks

**Short reads and writes.** `os.pread` can return fewer bytes than asked and `os.pwrite` can
write fewer. Both are checked and raise; a silent short page would corrupt search results.

**A syscall on the hot path.** The codebase deliberately avoids one per `retrieve` for
locking, so this is a real change of posture. Mitigated by the measurement above, and by the
fact that the syscall replaces a page fault that was never free either.

**Blocking becomes visible.** A mapping fault blocks while holding the GIL; a positioned
read releases it, which is a mild improvement under threads. But `test/conftest.py` defines
an unused `blockbuster` fixture, and if it is ever switched on it will now see a genuine
blocking syscall where a fault was invisible. Worth checking during implementation whether
any async handler reaches `retrieve` without `asyncio.to_thread`; the Super Search path goes
through `to_thread` at `mwmbl/tinysearchengine/super_search_sources/mwmbl_index.py:16`.

**The saving might not materialise** if something else maps the index. `VmPTE` after deploy
is the direct check. The bloom filters in `mwmbl/crawler/urls.py` map about 2.3 GB, which
costs only a few MB of page tables, so they are not worth touching.

## Rollout

Beta first, which is a genuine canary here because it is a second full copy of the app on
the same host and the same index.

1. Open the pull request. `build.yml` publishes a `pr-<N>` image to GHCR, built on GitHub's
   runners rather than the memory-starved server.
2. Run the `Deploy Beta` workflow manually with that tag.
3. Leave it under real traffic for a day. Check `VmPTE` across beta's workers, compare
   search latency, and watch the kernel log.
4. Merge, which deploys api through `deploy.yml`.

## Also to do

Post the findings to issue #374: that the kills are host-wide rather than container-limited,
the page table arithmetic and the measured 802 MiB, the two-container multiplier, and the
correction that the ~33,000 event extrapolation in the issue body came from reading the
trace sample rate rather than the error sample rate, which is not set and defaults to 1.0.
Blocked while plan mode is active.
