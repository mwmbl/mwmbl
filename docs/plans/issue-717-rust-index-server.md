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
  PR 4). The server knows pages, terms and documents and nothing of mwmbl's curation
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

## PR 2: Write pages in Python's array format (Done)
**Estimated added lines:** ~110
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/partition.rs, rust/mwmbl_core/tests/indexer.rs

`mwmbl_core` wrote each document on a page as a JSON object, while Python writes and
reads `Document.as_tuple()` arrays. Python can't read a page Rust wrote, and the field
names cost page bytes, so a Rust-written page holds fewer documents. Pages are written
through `PageItem`, which serialises a `Document` as that array, in the same field order
with the trailing Nones cut off. The HTTP API keeps objects, and reading already accepts
both forms.

- A page Rust writes is byte-identical to the one Python writes for the same documents.

## PR 3: Index server crate with the indexing operations (Done)
**Estimated added lines:** ~450
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/partition.rs, rust/index_server/{Cargo.toml,src/lib.rs,src/api.rs,src/ownership.rs}, mwmbl_rank/Cargo.toml, mwmbl_rank/src/lib.rs, Dockerfile

An axum app on a Unix socket with `POST /v1/{index}/documents` (`index_documents`, which
returns `stored_urls`, `new_urls`, `new_page_doc_counts` and `agreements`),
`POST /v1/{index}/pages` (`index_pages`, with an optional `mark_synced` term),
`POST /v1/{index}/retrieve` (several terms in one call) and `GET /v1/health`. `{index}` is
`main` or `external_cache`; both paths are given at startup.

`from_file` gains a lock policy. With `Fail`, which the crawler uses, a second writer is
an error as now. With `Wait`, which the server uses, the channels exist at once and the
owner threads block on the `flock` before their receive loop.

`ownership.rs` handles the deploy handover. The server binds and answers at once (health
never waits for the lock), and requests queue in the partition channels until the lock is
taken. On SIGTERM it stops accepting, drops its index handles and joins the owner threads
off the runtime (`spawn_blocking`), so their queues drain, and then exits. That releases
the lock to the new container.

Expose `mwmbl_rank.serve_index(socket_path, index_paths, num_threads)`, which runs it
with the GIL released. Copy `rust/index_server` in the Dockerfile builder stage.

- Rust tests drive the router with `tower::ServiceExt::oneshot`, with no socket.
- A second server on the same file answers health but applies no writes until the first
  exits, and then applies the queued ones.
- SIGTERM with writes queued applies them before exit.

## PR 4: Conditional page reads and writes (Done)
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

## PR 5: Python client and server startup (Done)
**Estimated added lines:** ~390
**Files:** mwmbl/indexer/index_client.py, mwmbl/main.py, mwmbl/settings_common.py, test/conftest.py, test/test_index_client.py, test/test_index_server.py

`IndexClient` wraps the endpoints and returns the same `IndexedDocuments` and `Counter`
types as today. `stored_urls` and `new_urls` map to `IndexedURLs`. The page read and write
are thin, and pass documents through as the server sends them, `extra` included, for
PR 8 to build on. Requests retry with backoff for a bounded time, which covers restarts
and the handover, and then raise `IndexServerUnavailable`. `main.py` starts the server
before gunicorn when `RUN_INDEX_SERVER` is set. It defaults off, and must not be turned on
in production until PR 8 has landed. Until then, Python still writes the same file under
OFD locks that the owner threads don't take, and an owner thread that reads a page while
Python is writing it would see an undecodable page and reset it. The socket goes in
`INDEX_SERVER_SOCKET`, which must be in a directory local to the container, never on a
shared volume, or the old and new containers' servers would share it. A pytest fixture
starts the server in a spawned process on a temporary socket, as `main.py` does.

Lifecycle and failure handling:
- `main.py` forwards SIGTERM to the server child from gunicorn's `on_exit` hook, which
  runs in the master alone once the workers have stopped, and waits for it to drain within
  `INDEX_SERVER_STOP_SECONDS`. gunicorn's graceful timeout is set explicitly, and the
  container's stop timeout must cover both. The child gets SIGTERM if its parent dies
  (`PR_SET_PDEATHSIG`, set with `prctl` in the child before it serves).
- gunicorn's master reaps every child of its own, the server among them, so the master
  waits on the process's sentinel rather than its exit code. Starting waits for the socket
  and fails if the child exits first, and a server that exits unasked stops gunicorn with
  an error, so the container restarts.
- Workers clear multiprocessing's inherited record of the master's children after fork,
  or a worker's exit handlers would terminate the task queue and seed crawl processes.
- Client timeouts exceed the handover window. A retried write can be applied twice, so
  the stats from a retried request may undercount `new_urls`; the client documents this.
- Batches are chunked to the server's 32 MiB body limit.

## PR 6: Make Rust indexing match Python
**Estimated added lines:** ~300
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/agreement.rs, rust/mwmbl_core/src/index.rs, rust/mwmbl_core/tests/…, rust/crawler/Cargo.toml

Indexing the same batches through the old Python path and through the server gives
different pages, counts and agreements. Each difference is a change to the crawler's own
indexing too, so raise the crawler version.
- URLs with no domain (IP hosts, unparseable URLs): Rust's `index_documents` drops them
  and Python keeps them. Check the blacklist only for a URL with a domain.
- `trigram_similarity` gives NaN for two empty strings, where Python gives 1.0, and is
  wrong for strings shorter than a trigram (it counts bytes, and `len() - 2` underflows).
  A string shorter than a trigram is its own only gram, as in Python.
- user_ids: Rust merges them ordered by crawl time and keeps all of them. Python merges
  them in rank order with the kept text's author last, and keeps `MAX_USER_IDS` (2). The
  agreements rely on the last id being the author.
- Title dedup: Rust merges the user_ids of a document with the same title and another URL
  into the one it keeps. Python drops the later one.
- Page order: documents of different terms come out in HashMap order, so a page's order
  isn't deterministic. Break ties as Python does, by the order the terms were first met.
- Tokenization, **pending a decision from the maintainer**: Rust indexes every token and
  splits a URL on every character but an ASCII letter or digit. Python takes the first 10
  tokens and 10 bigrams of each field, splits a URL only on `/._`, and unquotes it first.
  Matching Python keeps production's terms as they are; the alternative is to write down
  Rust's as intended.

- Once these are fixed, any difference left in which copy of a URL a page keeps is
  explained by ranking, or fixed.
- `make rust-core` passes.

## PR 7: Batch indexing through the server
**Estimated added lines:** ~180
**Files:** mwmbl/indexer/index_batches.py, mwmbl/indexer/index_client.py, mwmbl/crawler/app.py, test/test_index_client.py, test/test_index_server_parity.py

With `RUN_INDEX_SERVER` on, `index_documents` and `index_pages` in `index_batches.py` call
the client, and `/documents` is only called after `filter_blacklisted_documents` and text
cleaning. The old Python path stays until PR 9 deletes it; a setting chooses between them.
POST /crawler/results answers 503 when the server is unavailable after the retries, and
the crawler retries.

- A parity test runs the same fixture batches through the old Python path and through
  the server, and compares the pages, `new_page_doc_counts`, URLs and agreements. It
  covers URLs with no domain (IP hosts, unparseable URLs). Any difference left after PR 6
  is fixed in Rust, or written down as intended.
- `make check` and `make test` pass.

## PR 8: Curation, purge, external cache and widgets through the server
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

Carrying documents through the client:
- The server returns a page item's elements beyond the nine it knows as `extra`. The
  client keeps them on the documents it read, as a field on `Document` or alongside it,
  and sends them back, or a write would drop what a newer build stored.
- A write stores only the documents that fit and returns `num_stored`, dropping the tail
  as `store()` does today. A caller that prepends to a page checks it.
- When the attempts run out, `page()` logs the page and the number of attempts before it
  raises.
- Scores round-trip through the server as `f32`. Curated scores are small integers, which
  survive exactly; other scores may lose precision past seven digits.

- Nothing on the server opens the index in `"w"` mode any more, except offline tools
  (`restore_index`, `create_index`).
- A test races two `page()` edits on one page through the server and keeps both.

## PR 9: Delete Python page locking
**Estimated added lines:** ~50 (mostly deletions)
**Files:** mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_batches.py, mwmbl/tinysearchengine/copy_index.py, test/…

Remove `_locked_page`, the OFD constants and `_set_page_lock`, the fallbacks for running
unlocked, the file-backed write path (`"w"` mode, `store_in_page`, `_write_page`), and
the Python `combine_documents` and `_store_pages` once nothing calls them. `page()` stays,
on the client. `copy_index` is deprecated: delete it, or point it at the
client. Search keeps its lock-free direct reads, which treat a `PageError` as an empty
term, as now.

- No `fcntl` page locking is left in the codebase.

## PR 10: Search reads through the server
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
