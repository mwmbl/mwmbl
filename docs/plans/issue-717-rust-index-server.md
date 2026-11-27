# Issue #717: Rust index server as the single writer to the index

The Rust crawler produces results faster than the Python server can index them, and the
Rust `TinyIndex` can't run on the server because it allocates one `RwLock` per page (~6–8 GB
at 102M pages). Today several Python processes write the index and coordinate with OFD
page locks. When this is finished, `mwmbl_core`'s `TinyIndex` is a lock-free, partitioned
handle: page `p` is only ever read or written by owner thread `p % K`, so there are no
page locks anywhere. The crawler uses that handle on its own index file. On the server, a
Rust index server built on it owns both the main index and the external cache, and it is
the only writer. It is an axum HTTP/JSON server on a Unix socket, started as a child
process of the existing app, and every Python writer is a client of it. All Python page
locking is deleted, and search can read through the server as well.

Design decisions, from the issue thread:
- **Lock-free partitioning.** Each owner thread is a dedicated OS thread fed by a
  `std::sync::mpsc` channel, and it answers on a `tokio::sync::oneshot`. A thread drains
  its queue and groups the requests for one page into a single read-modify-write. Reads
  are requests on the same channel, so a read never overlaps a write and always sees the
  writes queued before it. That keeps the crawler's index-then-retrieve sequence correct.
- **Writes are operations, never finished pages.** A client never does read-modify-write.
- **The only lock is one whole-file `flock(LOCK_EX)`,** taken once at startup. It stops
  two processes from owning one file: two crawlers sharing a home directory, or the old
  and new containers during a deploy. No request ever touches it.
- **Single writer, so an undecodable page is real damage.** The owner thread resets it,
  as Python does under its lock today, instead of skipping it.
- **HTTP, not gRPC.** Axum with JSON on a Unix socket, under `/v1/`. Python uses `httpx`
  (already a dependency) with `HTTPTransport(uds=...)`.
- **Started from `main.py`, before gunicorn forks,** like the task queue: a `spawn` child
  calls `mwmbl_rank.serve_index(...)`, which runs the server with the GIL released. The
  server code is in its own crate (`rust/index_server`) and `mwmbl_rank` links it, so the
  Dockerfile and the test setup (`maturin develop`) gain no new build.
- **High level for indexing, low level for everything else.** `index_documents` is a
  server operation, because it is the throughput bottleneck and only the server can
  group the updates to one page. Curation, purge, the external cache and widgets keep
  their Python logic, on a generic conditional page read and write (compare-and-swap,
  PR 3). The server knows pages, terms and documents and nothing of mwmbl's curation
  rules, so other projects, such as social, can build on it. The `Document` schema stays
  as it is until another project needs something different.
- **Python keeps the blacklist check.** It filters documents with the snapshot blacklist
  before sending them, so the server indexes with a provider that blocks nothing.

## PR 1: Partitioned, lock-free TinyIndex in mwmbl_core (Done)
**Estimated added lines:** ~450
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/partition.rs, rust/mwmbl_core/Cargo.toml, rust/crawler/src/crawler.rs, rust/crawler/Cargo.toml

Replace `page_locks`, `ReadPageLock`/`WritePageLock` and the `file_guard` byte-range locks
with `K` owner threads. `K` is a constructor argument, and the crawler gets a CLI flag
with a small default. `PageRequest` is an enum (`Retrieve { term }`, `Merge { docs,
mark_term_synced }`), not a boxed closure. `index_documents`, `index_pages` and `retrieve`
keep their async signatures: they split the work by owner, send it, and gather the
replies. A batch of `Merge`s for one page makes one read, one `combine_documents` over
all their documents, and one write. Take a whole-file `flock(LOCK_EX | LOCK_NB)` in
`from_file` for `WRITE` mode, and fail with a clear error if another process holds it.
Dropping the handle closes the channels and joins the threads. Raise the crawler version.

- No per-page memory: opening a 102M-page index allocates nothing proportional to it.
- Concurrent `index_documents` calls on overlapping pages lose no documents (test with
  many tasks on a small index).
- A `retrieve` sent after an `index_pages` on the same page sees its documents.
- An undecodable page is reset on write, logged, and not skipped.
- A second `WRITE` open of the same file fails while the first is alive.
- `make rust-core` passes.

## PR 2: Index server crate with the indexing operations
**Estimated added lines:** ~450
**Files:** rust/index_server/{Cargo.toml,src/lib.rs,src/api.rs,src/ownership.rs}, mwmbl_rank/Cargo.toml, mwmbl_rank/src/lib.rs, Dockerfile

An axum app on a Unix socket with `POST /v1/{index}/documents` (`index_documents`, which
returns `stored_urls`, `new_urls`, `new_page_doc_counts` and `agreements`),
`POST /v1/{index}/pages` (`index_pages`, with an optional `mark_synced` term),
`POST /v1/{index}/retrieve` (several terms in one call) and `GET /v1/health`. `{index}` is
`main` or `external_cache`; both paths are given at startup.

`ownership.rs` handles the deploy handover. The server binds and answers at once (health
never waits for the lock), while a background thread blocks on the `flock`. Requests
queue in the partition channels until the lock is taken and the owner threads start. On
SIGTERM it stops accepting, drains its queues and exits, which releases the lock to the
new container.

Expose `mwmbl_rank.serve_index(socket_path, index_paths, num_threads)`, which runs it
with the GIL released. Copy `rust/index_server` in the Dockerfile builder stage.

- Rust tests drive the router with `tower::ServiceExt::oneshot`, with no socket.
- A second server on the same file answers health but applies no writes until the first
  exits, and then applies the queued ones.
- SIGTERM with writes queued applies them before exit.

## PR 3: Conditional page reads and writes
**Estimated added lines:** ~350
**Files:** rust/mwmbl_core/src/indexer.rs, rust/index_server/src/api.rs, test/…

Low-volume edits (curation, purge, the external cache, widgets) stay in Python, on two
generic primitives:
- `GET /v1/{index}/page/{i}` returns the page's documents and its version: a hash of the
  page's bytes as stored.
- `PUT /v1/{index}/page/{i}?if_version=v` writes the documents only if the page's current
  bytes still hash to `v`. It returns how many documents fit, or 409 with nothing
  written. The owner thread re-reads the page and compares the hash itself, so the
  server keeps no per-page state. Identical bytes mean identical contents, so there's no
  ABA problem.

An undecodable page reads as empty, with a version that is the hash of its corrupt
bytes. A caller's write therefore resets it, as Python's locked `page()` does today.

- Two clients racing a read-modify-write on one page both land: one gets 409, retries
  and succeeds.
- A write against a stale version changes nothing on disk.

## PR 4: Python client; batch indexing through the server
**Estimated added lines:** ~450
**Files:** mwmbl/indexer/index_client.py, mwmbl/main.py, mwmbl/settings_common.py, mwmbl/indexer/index_batches.py, mwmbl/crawler/app.py, test/conftest.py, test/test_index_client.py, test/test_index_server_parity.py

`IndexClient` wraps the endpoints and returns the same `IndexedDocuments` and `Counter`
types as today. Writes retry with backoff for a bounded time, which covers restarts and
the handover, and then raise. POST /crawler/results answers 503, and the crawler
retries. `main.py` starts the server before gunicorn when `RUN_INDEX_SERVER` is set. The
socket goes in `INDEX_SERVER_SOCKET`, a directory local to the container.
`index_documents` and `index_pages` in `index_batches.py` call the client, and they keep
the blacklist filter and text cleaning in Python. A pytest fixture starts
`serve_index` in a spawned process on a temporary socket.

- A parity test runs the same fixture batches through the old Python path and through
  the server, and compares the pages, `new_page_doc_counts`, URLs and agreements. Any
  difference is fixed in Rust here, or written down as intended.
- `make check` and `make test` pass.

## PR 5: Curation, purge, external cache and widgets through the server
**Estimated added lines:** ~250
**Files:** mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_client.py, mwmbl/views.py, mwmbl/background.py, mwmbl/indexer/purge_blacklisted.py, mwmbl/indexer/external_cache.py, mwmbl/management/commands/index_widgets.py, test/…

Rebuild `page()` on the client as a compare-and-swap loop. It reads the page and version,
runs the caller's block, and writes with `if_version`. On a 409 it runs the block again
on a fresh read, up to a bounded number of attempts, and then raises. The callers'
read-modify-write code and tests stay as they are; they change from a locked file to
the client. The external cache keeps "never raises": a server error is logged and the
search goes on. The block may run more than once, so it must have no side effects
beyond the page. Check each caller for this (for example, purge's removal counts must
come from the final attempt).

- Nothing on the server opens the index in `"w"` mode any more, except offline tools
  (`restore_index`, `create_index`).
- A test races two `page()` edits on one page through the server and keeps both.

## PR 6: Delete Python page locking
**Estimated added lines:** ~50 (mostly deletions)
**Files:** mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_batches.py, mwmbl/tinysearchengine/copy_index.py, test/…

Remove `_locked_page`, the OFD constants and `_set_page_lock`, the fallbacks for running
unlocked, the file-backed write path (`"w"` mode, `store_in_page`, `_write_page`), and
the Python `combine_documents` and `_store_pages` once nothing calls them. `page()` stays,
on the client. `copy_index` is deprecated: delete it, or point it at the
client. Search keeps its lock-free direct reads, which treat a `PageError` as an empty
term, as now.

- No `fcntl` page locking is left in the codebase.

## PR 7: Search reads through the server
**Estimated added lines:** ~250
**Files:** mwmbl/search_setup.py, mwmbl/tinysearchengine/rank.py, mwmbl/indexer/index_client.py, scripts/bench_retrieve.py, test/…

Behind `INDEX_READS_VIA_SERVER`, the ranker gets all of a query's terms in one
`retrieve` call. Benchmark p50 and p99 search latency both ways on production-sized
data, and switch the default only if reads through the server are no slower. With reads
in the server there are no torn reads, so the corruption fallback goes. Retrieving and
ranking inside the server (`mwmbl_rank`) is a follow-up issue. This PR deletes this plan
and says `Closes #717`.

- Benchmark results are in the PR description.
- With the flag on, search makes no direct reads of the index file.
