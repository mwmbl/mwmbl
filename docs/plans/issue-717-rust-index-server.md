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
- **Python keeps the blacklist check.** It filters documents with the snapshot blacklist
  before sending them, so the server indexes with a provider that blocks nothing.

## PR 1: Partitioned, lock-free TinyIndex in mwmbl_core
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

## PR 3: Curation and purge operations
**Estimated added lines:** ~400
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/edits.rs, rust/index_server/src/api.rs

New `PageRequest` variants and endpoints, each a port of the Python read-modify-write it
replaces:
- `curate(term, documents, states)`: `views._save_to_index`, including `add_term_infos`,
  which infers a missing term from the document's tokens and the page index.
- `replace_term(term, documents)`: `views._revert_curation`.
- `purge(urls)`: `purge_blacklisted.purge_documents`. It computes each URL's pages from
  its tokens, rewrites only the pages that change, and returns the removed URLs.

- Each operation has a Rust test ported from the Python test of the code it replaces.
- A purge that changes nothing on a page doesn't rewrite it.

## PR 4: External cache and widget operations
**Estimated added lines:** ~350
**Files:** rust/mwmbl_core/src/edits.rs, rust/index_server/src/api.rs

- `cache_store(term, source, entries, now, ttl)`: the `external_cache` write. It drops
  this source's old entries for the term and any stale entries, then orders newest-first,
  so that eviction falls on the oldest.
- `remove_widgets(keys, wanted)`: `index_widgets.remove_stale_widgets`. Only documents
  whose source is a widget are touched.

- Ported tests cover eviction order, expiry, and the case where providers share a term.

## PR 5: Python client; batch indexing through the server
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

## PR 6: Curation, purge, external cache and widgets through the server
**Estimated added lines:** ~300
**Files:** mwmbl/views.py, mwmbl/background.py, mwmbl/indexer/purge_blacklisted.py, mwmbl/indexer/external_cache.py, mwmbl/management/commands/index_widgets.py, test/…

Each caller swaps `with index.page(...)` for its client operation. The external cache
keeps "never raises": a server error is logged and the search goes on.

- Nothing on the server opens the index in `"w"` mode any more, except offline tools
  (`restore_index`, `create_index`).

## PR 7: Delete Python page locking
**Estimated added lines:** ~50 (mostly deletions)
**Files:** mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_batches.py, mwmbl/tinysearchengine/copy_index.py, test/…

Remove `_locked_page`, the OFD constants and `_set_page_lock`, the fallbacks for running
unlocked, `_OpenPage` and `page()`, and the Python `combine_documents` and `_store_pages`
once nothing calls them. `copy_index` is deprecated: delete it, or point it at the
client. Search keeps its lock-free direct reads, which treat a `PageError` as an empty
term, as now.

- No `fcntl` page locking is left in the codebase.

## PR 8: Search reads through the server
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
