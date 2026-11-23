# Issue #694: Scale up crawling by merging pages from crawlers sequentially

Today every crawler pushes batches to `POST /api/v1/crawler/results`. The server
re-tokenises each batch and merges it page by page into the main index at random positions,
then sends the main index's results for those terms back. One fast Rust crawler is already
more than the server can keep up with. When this is finished the server pulls instead. A
crawler polls `POST /api/v1/crawler/sync/poll` every ~10 seconds. The server answers either
`idle` or "send me pages `[start, end)` of your local index for round R". Each invited
crawler posts the unsynced documents on those pages to
`POST /api/v1/crawler/sync/rounds/{R}/pages`, or an empty list. When every invited crawler
has answered, or the round's deadline passes, a single coordinator process merges all the
submissions into the main index and moves the cursor on. Rounds walk the crawler index from
page 0 to the end and then wrap, so each main index page is written once per cycle. The
batch size is chosen to keep memory bounded. Crawlers no longer push, and they no longer get
the main index's results back.

**Index geometry.** A crawler page `c` holds terms with `murmur3(term) % C == c`, and a main
page `m` holds terms with `murmur3(term) % M == m` (the same hash on both sides: `mmh3`
unsigned in Python, `murmurhash3_with_seed::<0>` in Rust). If `C` divides `M`, then
`m % C == c`, so crawler pages `[a, b)` map onto exactly the main pages
`{m : m % C in [a, b)}`. Across a cycle every main page is touched in exactly one round.
Production has `M = 102_400_000`. The crawler index is `C = 100_000` today and becomes
**`C = 1_024_000`** (about 4.2 GB, and `M / C = 100`). "Exactly 10x" (1,000,000) does not
divide `M`, so it is not used. Correctness does not depend on this: the merge rehashes
each document's term to find its main page. Only the "each main page once per cycle"
property does, so dev and test settings with a small `M` still work.

**Constants** (server settings, in `settings_common.py`):

| Setting | Default | Meaning |
|---|---|---|
| `CRAWLER_INDEX_NUM_PAGES` | `1_024_000` | `C`. A poll from a crawler whose index differs gets a 400. |
| `CRAWLER_SYNC_ENABLED` | `False` (prod: `True` in the last PR) | Starts the coordinator process. |
| `CRAWLER_SYNC_POLL_SECONDS` | `10` | `poll_after_seconds` sent to crawlers. |
| `CRAWLER_SYNC_ACTIVE_SECONDS` | `60` | A device that polled this recently is invited to the next round. |
| `CRAWLER_SYNC_ROUND_SECONDS` | `30` | The round deadline, measured from when it opens. |
| `CRAWLER_SYNC_MEMORY_BUDGET_BYTES` | `512 * 2**20` | Caps `pages_per_round * active_devices * PAGE_SIZE`. |
| `CRAWLER_SYNC_MIN_PAGES` / `MAX_PAGES` | `16` / `4096` | Bounds on pages per round. |
| `CRAWLER_SYNC_MAX_DOCUMENTS_PER_PAGE` | `200` | Per page, per submission. |

## PR 1: Page-level access to the crawler's local index in mwmbl_core
**Estimated added lines:** ~250
**Files:** rust/mwmbl_core/src/indexer.rs, rust/mwmbl_core/src/lib.rs (if exports change)

Add public, page-addressed operations to `TinyIndex`. Today only term-addressed
`retrieve` and the private `get_page`/`store_page` exist.

```rust
impl TinyIndex {
    #[must_use]
    pub fn num_pages(&self) -> usize;

    /// Every document on `page`, under its read lock.
    pub async fn read_page(&self, page: usize) -> anyhow::Result<Vec<Document>>;

    /// Under the page's write lock, set `state = SyncedWithMainIndex` on the documents whose
    /// (url, term) is in `sent`. Documents replaced since they were read (a re-crawl changes
    /// the stored copy) are left unsynced, so they are sent in the next cycle. Returns how many
    /// it marked.
    pub async fn mark_synced(
        &self,
        page: usize,
        sent: &HashSet<(String, String)>,
    ) -> anyhow::Result<usize>;
}

/// Copy every unsynced document of the index at `old` into the index at `new` (which may
/// have a different number of pages), rehashing each by its term. It reads one old page at a
/// time and buffers writes per new page in chunks, so memory stays bounded.
pub async fn migrate_index(old: &Path, new: &Path) -> anyhow::Result<usize>;
```

No caller in the crawler yet. The commit message carries `No functional changes to the crawler.`

- `read_page` returns documents of every term on the page. Tested against an index
  built with `index_documents`.
- `mark_synced` marks only the matching (url, term) pairs, and leaves a copy that was
  replaced between the read and the mark alone.
- `migrate_index` from a 10-page to a 37-page index puts every unsynced document on the page
  its term hashes to, and drops synced ones.
- `make rust-core` passes.

## PR 2: Grow the crawler index to 1,024,000 pages
**Estimated added lines:** ~150
**Files:** rust/crawler/src/constants.rs, rust/crawler/src/main.rs, rust/crawler/src/path_creation.rs, rust/crawler/README.md, rust/crawler/Cargo.toml

- `NUM_PAGES = 1_024_000`. Add `INDEX_NAME = "crawl-index-v2.tinysearch"` and keep the old
  name as `LEGACY_INDEX_NAME`.
- At startup, if the legacy file exists and the v2 one does not, create v2 and call
  `migrate_index`. Then delete the legacy file. Log progress so a volunteer can see why
  startup is slow.
- Raise the crawler version (2.0.8 → 2.1.0). The README says the index now needs about 4.2 GB
  of disk.
- The behaviour is otherwise unchanged: it still pushes to `POST /results`, which does not
  care about the local index size.

- A test in `path_creation`/`main` shows that a legacy index is migrated and removed, and that
  an existing v2 index is left alone.
- `make rust-core` passes, and the release check is satisfied by the version bump.

## PR 3: Sync rounds model, settings and the poll endpoint
**Estimated added lines:** ~420
**Files:** mwmbl/models.py, mwmbl/migrations/00xx_crawler_sync.py, mwmbl/settings_common.py, mwmbl/crawler/sync.py (new), mwmbl/crawler/batch.py, mwmbl/crawler/app.py, rust/crawler/fixtures/sync_poll_request.json, rust/crawler/fixtures/sync_poll_response.json, test/test_crawler_sync_poll.py, test/test_rust_crawler_contract.py

Models:

```python
class CrawlerSyncCursor(models.Model):
    """Singleton (pk=1): where the next round starts."""
    cycle = models.IntegerField(default=0)
    next_page = models.IntegerField(default=0)

class CrawlerSyncRound(models.Model):
    cycle = models.IntegerField()
    start_page = models.IntegerField()        # inclusive
    end_page = models.IntegerField()          # exclusive
    invited_count = models.IntegerField()
    opened_at = models.DateTimeField()
    deadline = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)   # no submissions accepted after this
    merged_at = models.DateTimeField(null=True)
    num_documents = models.IntegerField(default=0)
    num_new_urls = models.IntegerField(default=0)

class CrawlerSyncSubmission(models.Model):
    round = models.ForeignKey(CrawlerSyncRound, on_delete=models.CASCADE, related_name="submissions")
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    received_at = models.DateTimeField()
    num_documents = models.IntegerField()
    spool_path = models.CharField(max_length=500, blank=True)   # "" for an empty submission
    class Meta:
        unique_together = ("round", "device")
```

`mwmbl/crawler/sync.py` holds the round logic shared by the endpoints and the coordinator:
`current_open_round()` (cached in the Django cache for 1 s, because every crawler polls it),
`mark_active(device)` and `active_device_count()` (a Redis sorted set
`crawler-sync:active`, scored by poll time), and `crawler_page(term) -> int`.

API, under the existing crawler router:

```
POST /api/v1/crawler/sync/poll
X-API-Key: <crawl-scoped key>
```

```python
class SyncPollRequest(Schema):
    crawler_version: str
    device_name: str
    num_pages: int = Field(description="Pages in the crawler's local index; must equal CRAWLER_INDEX_NUM_PAGES.")

class SyncPollResponse(Schema):
    status: Literal["idle", "send"]
    poll_after_seconds: float
    round_id: int | None = None
    start_page: int | None = None          # inclusive
    end_page: int | None = None            # exclusive
    deadline: float | None = None          # Unix time after which the round refuses submissions
    max_documents_per_page: int | None = None
```

Responses are `200 SyncPollResponse`, `400 Error` (`num_pages` mismatch), `401 Error` and
`426 Error` (below `MIN_CRAWLER_VERSION`, the same check as `/results`, factored out).
The endpoint calls `record_device` and `mark_active`. It answers `send` when a round is open
and this device has not yet submitted to it, and `idle` otherwise.

- Tests cover idle without a round, `send` with the round's range, `idle` after the device
  has submitted, the 400, 401 and 426 responses, and activity being recorded.
- The contract fixtures validate against the schemas, as in `test_rust_crawler_contract.py`.

## PR 4: The page submission endpoint
**Estimated added lines:** ~400
**Files:** mwmbl/crawler/app.py, mwmbl/crawler/batch.py, mwmbl/crawler/sync.py, rust/crawler/fixtures/sync_pages_request.json, rust/crawler/fixtures/sync_pages_response.json, test/test_crawler_sync_pages.py, test/test_rust_crawler_contract.py

```
POST /api/v1/crawler/sync/rounds/{round_id}/pages
X-API-Key: <crawl-scoped key>
```

```python
class SyncDocument(Schema):
    url: str
    title: str
    extract: str
    term: str
    last_crawled: int | None = None

class SyncPage(Schema):
    page: int
    documents: list[SyncDocument]

class SyncPagesRequest(Schema):
    crawler_version: str
    device_name: str
    pages: list[SyncPage] = Field(description="Only unsynced documents; an empty list means nothing to send.")

class SyncPagesResponse(Schema):
    status: Literal["accepted"]
    num_accepted: int
    num_rejected: int
```

The responses:

- `200 SyncPagesResponse`.
- `400 Error`: a page outside `[start_page, end_page)`, too many documents on a page, or a
  `last_crawled` in the future.
- `401 Error`.
- `409 Error`: the round is closed, is not the open one, or this device already submitted to
  it. The crawler keeps its documents unsynced and sends them next cycle.
- `426 Error`.

Validation is the trust and safety boundary, so a crawler cannot use a page to place
documents under terms they do not contain:

- A document is accepted only if `crawler_page(term) == page` and `term` is one of
  `tokenize_document(url, title, extract)`'s tokens. Others count in `num_rejected`.
- Titles and extracts are cut with `_truncated` as in `/results`, and the URL must be
  http(s).
- The server sets `user_ids` to `[api_key.user.id]` and ignores anything the crawler claims.
- The blacklist is applied at merge time (PR 6), through `filter_blacklisted_documents`.

The accepted documents are written gzipped to
`{DATA_PATH}/crawler-sync/{round_id}/{device_id}.json.gz` before the
`CrawlerSyncSubmission` row is inserted. Inserting the row takes `select_for_update` on the
round and checks `closed_at IS NULL`, so a submission racing the close either lands or gets
a 409. It is never half-counted. An empty `pages` inserts a row with an empty `spool_path`,
so the round can close early.

- Tests cover acceptance and the spooled file's contents, each 400, each 409, rejection of a
  term that hashes elsewhere or is not in the document, and an empty submission.
- The contract fixtures pass.

## PR 5: The sync coordinator: opening, closing and advancing rounds
**Estimated added lines:** ~350
**Files:** mwmbl/crawler/sync_coordinator.py (new), mwmbl/main.py, mwmbl/settings_common.py, test/test_crawler_sync_coordinator.py

`run_crawler_sync()` runs as a single process, started from `main.py` beside
`run_seed_crawls` when `CRAWLER_SYNC_ENABLED`. It loops over one step:

```python
def step(now: datetime) -> None:
    """Close the open round if it is due, merge it (PR 6: a no-op hook here), and otherwise
    open the next round when there are active devices."""

def pages_per_round(active_devices: int) -> int:
    """MEMORY_BUDGET // (active_devices * PAGE_SIZE), clamped to [MIN_PAGES, MAX_PAGES]
    and to the pages left before the end of the crawler index."""
```

- Opening a round records `invited_count = active_device_count()` and a deadline. Opening
  nothing when no device is active.
- A round closes when it has `invited_count` submissions or passes its deadline, whichever
  comes first.
- After the merge, the cursor advances to `end_page`. At `CRAWLER_INDEX_NUM_PAGES` it wraps to
  0 and `cycle += 1`.
- A restart resumes from the database. A round left open is closed and merged, and one
  closed but not merged is merged.

- Tests drive `step` with a fake clock: opening, early close, deadline close, wrap-around,
  the budget clamp, and resuming after a crash.

## PR 6: Merge closed rounds into the main index
**Estimated added lines:** ~400
**Files:** mwmbl/crawler/sync_merge.py (new), mwmbl/crawler/sync_coordinator.py, mwmbl/indexer/index_batches.py, test/test_crawler_sync_merge.py

```python
def merge_round(round: CrawlerSyncRound, index_path: str) -> MergeResult:
    """Read the round's spooled submissions, group the documents by
    TinyIndex.get_key_page_index(term), and store them with index_batches._store_pages in
    chunks of main pages, so each main page in the round is written once. Then delete the
    spool directory."""
```

- Documents go through `filter_blacklisted_documents` and `cleaned_document` first, the same
  choke point as `index_documents`. Factor out a helper that both share, rather than copying
  it.
- The per-submitter accounting matches what `POST /results` does today: `new` URLs counted
  per device (`record_device_results`), `stats_manager.record_results`, `record_agreements`
  and `record_worst_agreements` from the stored copies, and `ApiKey.last_used`.
- Each submission is uploaded with `upload_object(..., "sync")`, without any key, so the
  public batch archive continues.
- The round's `num_documents` and `num_new_urls` are set and `merged_at` is stamped.

- Tests show that documents from two devices for the same term land on the right main page,
  ranked together, with both users' ids merged. They also cover a blacklisted domain being
  dropped, device counts, the spool being deleted, and each main page being written once per
  round.

## PR 7: The crawler syncs by polling instead of pushing
**Estimated added lines:** ~450
**Files:** rust/crawler/src/sync.rs (new), rust/crawler/src/batch.rs, rust/crawler/src/crawler.rs, rust/crawler/src/constants.rs, rust/crawler/src/main.rs, rust/crawler/README.md, rust/crawler/Cargo.toml

- `Crawler::index` keeps batching crawled documents into the local index
  (`index_documents`) but stops calling `POST /results`. The `new_items` round trip goes too.
- A new `sync` task runs this loop:
  1. Poll, then sleep `poll_after_seconds`.
  2. On `send`, `read_page` each page in `[start, end)` and keep the documents whose state is
     not `SyncedWithMainIndex`, capped at `max_documents_per_page`.
  3. POST them before the `deadline`.
  4. On 200, `mark_synced` exactly what was sent. On 409, drop the round. On 426, pause as
     the push path does now (`UPGRADE_REQUIRED_WAIT`).
- Serde types `SyncPollRequest`, `SyncPollResponse`, `SyncPagesRequest` and
  `SyncPagesResponse` are pinned by the fixtures from PRs 3 and 4.
- The crawler version goes to 2.2.0.

- Fixture tests show the requests serialise to the pinned JSON and the responses parse.
- A `TestServer` test covers the flow: poll, send, mark synced, then a second poll in the
  same cycle sends nothing. On a 409 nothing is marked.
- `make rust-core` passes.

## PR 8: Turn on sync, add a status endpoint, and document it
**Estimated added lines:** ~200
**Files:** mwmbl/settings_prod.py, mwmbl/crawler/app.py, mwmbl/crawler/batch.py, mwmbl/crawler/README.md, test/test_crawler_sync_status.py, docs/plans/issue-694-pull-based-crawler-sync.md (deleted)

```
GET /api/v1/crawler/sync/status   (public)
```

```python
class SyncStatusResponse(Schema):
    cycle: int
    next_page: int
    num_pages: int                 # CRAWLER_INDEX_NUM_PAGES
    active_devices: int
    open_round: SyncRoundSummary | None
    last_round: SyncRoundSummary | None

class SyncRoundSummary(Schema):
    round_id: int
    start_page: int
    end_page: int
    invited_count: int
    num_submissions: int
    num_documents: int
    num_new_urls: int
    opened_at: float
    merged_at: float | None
```

- `CRAWLER_SYNC_ENABLED = True` in production.
- The crawler README describes the pull protocol and the geometry.
- `POST /results` stays for older crawlers. Whether to raise `MIN_CRAWLER_VERSION` to 2.2.0
  and stop the push load is left to the maintainer.
- This plan is deleted, and the pull request body says `Closes #694`.

- A status endpoint test covers no rounds, an open round, and a merged round.
