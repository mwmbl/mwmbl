# Issue #371: Enable ruff's DTZ rule and fix the naive datetime call sites

`uv run ruff check --no-cache --select DTZ --statistics` reports **65 violations across 21
files** today: 41 `DTZ003` (`datetime.utcnow()`), 9 `DTZ005` (`datetime.now()` with no
`tz`), 8 `DTZ011` (`date.today()`), 6 `DTZ001` (naive `datetime(...)`) and 1 `DTZ007`
(`strptime` without `%z`). They fall into four independent groups, each with a different
right answer, so the plan is one pull request per group and the rule goes into
`pyproject.toml` in the last one. When this is finished every datetime in the codebase is
explicitly UTC-aware, the `astimezone(timezone.utc).replace(tzinfo=None)` workaround
added by `fbb6c87` is gone, and `make check` fails on a new naive call site.

Two facts measured on `main` before writing this, both of which the sections below rely
on:

- `uv run pytest -W "error::RuntimeWarning"` — **713 passed, 1 failed**. The single
  failure is `test/test_domain_submissions.py::test_form_stores_bare_domain`:
  `DateTimeField DomainSubmission.submitted_on received a naive datetime ... while time
  zone support is active`. `USE_TZ = True` (`mwmbl/settings_common.py:114`), so the four
  Django ORM writes in group 1 are a live bug, not a style question.
- Formatting a naive vs. aware datetime into a Redis key changes the key:
  `str(datetime(2026, 9, 8, 13))` is `2026-09-08 13:00:00`, the aware equivalent is
  `2026-09-08 13:00:00+00:00`. `URL_HOUR_COUNT_KEY` is built this way, so group 3 has to
  preserve the string, not just the instant.

The replacement idioms were checked against the rule itself (`ruff check --isolated
--select DTZ` on a scratch file): `datetime.now(timezone.utc)`,
`datetime.now(timezone.utc).date()`, `datetime(..., tzinfo=timezone.utc)`,
`date.fromisoformat(...)`, `.strftime(...)` and `.replace(tzinfo=None)` are all clean.

Do not bulk-autofix. Run `uv run ruff check --no-cache --select DTZ --statistics` at the
end of each section and check the count has dropped by the number of sites that section
owns.

## PR 1: Make the Django ORM writes timezone-aware (Done)
**Estimated added lines:** ~70 (4 call sites, 2 imports, ~40 lines of test)
**Files:** mwmbl/views.py, mwmbl/background.py, pyproject.toml,
test/test_domain_submissions.py, test/test_curation_timestamps.py (new)

Four sites write a naive datetime straight into an aware `DateTimeField`. Use
`django.utils.timezone.now()`, which the codebase already uses for exactly this
(`mwmbl/models.py:130`, `mwmbl/platform/api.py:310`, `mwmbl/evaluation/api.py:93`) — not
`datetime.now(timezone.utc)`, so these sites read like their neighbours.

- `mwmbl/views.py:250` `submit_domain` → `DomainSubmission.submitted_on`
- `mwmbl/views.py:385` `save_curation` → `Curation.timestamp`
- `mwmbl/views.py:526` `flag_curation` → `CurationFlag.timestamp`
- `mwmbl/background.py:84` `copy_all_indexes` → `IndexInfo.last_copied_time`

Then add to `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
filterwarnings = ["error:.*received a naive datetime:RuntimeWarning"]
```

That is the gate DTZ cannot provide: `timezone.now()` and `datetime.utcnow()` are
indistinguishable to a linter at the point of assignment, and only Django knows the
column is aware. Verified that the ini form fails the run today and that the `-W`
command-line form does *not* (Python escapes a `-W` message, so the regex never matches)
— it has to go in `pyproject.toml`.

- `uv run pytest` is green with the new `filterwarnings` entry, i.e. no naive datetime
  reaches a `DateTimeField` anywhere in the suite
- `test_form_stores_bare_domain` asserts `submitted_on.tzinfo is not None`
- Nothing in `test/` exercises `save_curation` or `flag_curation` today (grep for
  `save_curation` returns no test), so add `test/test_curation_timestamps.py`: posting a
  curation and then flagging it stores aware `Curation.timestamp` and
  `CurationFlag.timestamp`. `test/test_copy_index.py` covers `copy_pages` but not
  `copy_all_indexes`, so `IndexInfo.last_copied_time` is covered by the
  `filterwarnings` gate alone
- `ruff check --select DTZ` count is 65 → 61

## PR 2: Carry the crawl time as aware UTC through the URL database and queue
**Estimated added lines:** ~120 (20 sites across 5 files)
**Files:** mwmbl/crawler/urls.py, mwmbl/redis_url_queue.py, mwmbl/indexer/update_urls.py,
test/test_url_database.py, test/test_crawl_functional.py

This is the cluster `fbb6c87` was fighting. `get_datetime_from_timestamp`
(`mwmbl/indexer/update_urls.py:159`) already returns an aware UTC datetime, but
`URLDatabase` and `RedisURLQueue` are naive, so `update_found_urls` converts the crawl
time down to naive to avoid `can't subtract offset-naive and offset-aware datetimes`.
Make the whole path aware instead and delete the conversion.

`FoundURL` is only constructed in `mwmbl/indexer/update_urls.py` (lines 85 and 120) and
in the two test files above, and `last_crawled` is only ever consumed by
`RedisURLQueue.queue_urls` — nothing is persisted, so the change is contained to these
five files. The `URLDatabase.urls` dict keys are datetimes but only their `.month` and
`.year` reach `settings.URLS_BLOOM_FILTER_PATH`, so making them aware does not move any
bloom filter file.

- `mwmbl/crawler/urls.py:64` `datetime.utcnow()` → `datetime.now(timezone.utc)`; `:68`
  and `:72` add `tzinfo=timezone.utc` to the `datetime(...)` constructors
- `mwmbl/crawler/urls.py:119` drop `.astimezone(timezone.utc).replace(tzinfo=None)` from
  `found_date = url.timestamp`, and rewrite the comment above it: `last_crawled` is now
  aware UTC everywhere rather than naive UTC everywhere
- `mwmbl/redis_url_queue.py:72` `datetime.utcnow() - url.last_crawled` →
  `datetime.now(timezone.utc) - url.last_crawled`
- `mwmbl/indexer/update_urls.py:44`, `:48`, `:97`, `:169`, `:171` are elapsed-time
  measurements → `datetime.now(timezone.utc)`; `:120` builds `FoundURL(url, "hn",
  URLStatus.NEW, datetime.now())` for HN links → `datetime.now(timezone.utc)`
- `test/test_url_database.py:104` and the four `datetime.utcnow()` timestamps, and the
  five in `test/test_crawl_functional.py`'s `sample_found_urls` fixture, become aware
- `test_crawl_date_from_a_real_batch_timestamp_is_naive` is the test written for
  `fbb6c87`; invert it — rename it and assert `last_crawled.tzinfo == timezone.utc` and
  `last_crawled == crawled_at`, keeping the `queue_urls` half that proves the subtraction
  in `queue_urls` still works end to end

- A `FoundURL` built from a real batch timestamp reaches `queue_urls` without a
  `TypeError`, and the just-crawled URL is still not re-queued
  (`get_domain_count("aware.example") == 0`)
- No `replace(tzinfo=None)` remains in `mwmbl/crawler/` or `mwmbl/indexer/`
- `ruff check --select DTZ` count is 61 → 41

## PR 3: Key the Redis daily and hourly counters off aware UTC
**Estimated added lines:** ~130 (27 sites across 5 files, plus one helper and its test)
**Files:** mwmbl/utils.py, mwmbl/crawler/stats.py, mwmbl/count_urls.py,
mwmbl/admin_views.py, mwmbl/crawler/app.py, test/test_stats_blacklisted_removed.py

The largest group: 14 sites in `mwmbl/crawler/stats.py`, 7 in `mwmbl/count_urls.py`, and
one each in `mwmbl/admin_views.py` and `mwmbl/crawler/app.py`. Almost all of them are
`datetime.utcnow().date()` or `date.today()` used to format a Redis key. Add

```python
def utc_today() -> date:
    return datetime.now(timezone.utc).date()
```

to `mwmbl/utils.py` and use it at every one of them — `mwmbl/count_urls.py` already
imports from that module (`parse_url`), and `mwmbl/crawler/stats.py` reaches it
transitively through `mwmbl.count_urls`, so this adds no import weight. Sites that are
measuring elapsed time rather than naming a day (`stats.py:108`, `:138`, `:325`, `:335`;
`count_urls.py:36`, `:38`, `:46`, `:91`) take `datetime.now(timezone.utc)` directly.

Two sites need more than a substitution:

- `mwmbl/crawler/stats.py:95` and `:191` build the hourly key with `datetime(y, m, d, h)`
  and format it into `URL_HOUR_COUNT_KEY`. Adding `tzinfo` here would change the key
  string (see the measurement above), silently zeroing the hourly crawl chart until the
  old keys expire — and splitting writer from reader if only one side is touched. Replace
  both `datetime(...)` constructions with
  `date_time.strftime("%Y-%m-%d %H:00:00")`, which produces the identical string, is
  DTZ-clean, and removes the constructor rather than annotating it.
- `mwmbl/crawler/stats.py:291` (`DTZ007`) parses a dataset's `%Y-%m-%d` string only to
  call `.date()` on it. Use `date.fromisoformat(hashed_dataset.date)`, which still raises
  `ValueError` so the existing fallback to `:294` is unchanged. While there, lift the
  method-local `from datetime import datetime` to the top of the file (AGENTS.md).

`mwmbl/admin_views.py:102` reads `BLACKLISTED_REMOVED_COUNT_KEY`, which
`stats.py:281` writes: both must move together or the admin page reads the wrong day's
key. `mwmbl/crawler/app.py:455` compares a batch's date string against "today" to decide
whether the S3 batch listing is safe to cache; batch prefixes are UTC, so `utc_today()`
is the correction, not a cosmetic change.

- A test asserts `URL_HOUR_COUNT_KEY` is still formatted as `url-count-hour-YYYY-MM-DD
  HH:00:00`, with no offset suffix
- The four `datetime.utcnow().date()` key lookups in
  `test/test_stats_blacklisted_removed.py` become `utc_today()`, and
  `test_the_purge_task_records_what_it_removed` still finds the count the purge wrote
- `_removed_counts` in the admin view reads the same key `record_blacklisted_removed`
  writes
- `ruff check --select DTZ` count is 41 → 14

## PR 4: Fix the remaining sites and turn DTZ on
**Estimated added lines:** ~70 (14 sites across 8 files, plus config and notes)
**Files:** pyproject.toml, notes/lint-and-typecheck-backlog.md,
mwmbl/indexer/index_batches.py, mwmbl/indexer/historical.py, mwmbl/crawl.py,
mwmbl/management/commands/train_domain_moderation_model.py, test/test_search_api_key.py,
analyse/update_urls.py, analyse/wiki_stats.py, scripts/moderation_eval.py

What is left is a long tail with no shared decision, so it goes in one pull request with
the config change the issue is actually asking for.

- `mwmbl/indexer/index_batches.py:64`, `:88` and `mwmbl/crawl.py:303` measure elapsed
  time (the last one builds the index-process crash history) → `datetime.now(timezone.utc)`
- `mwmbl/indexer/historical.py:12` walks back `DAYS` days of batch date strings to look
  up S3 prefixes, which are UTC → `datetime.now(timezone.utc).date()`
- `mwmbl/management/commands/train_domain_moderation_model.py:106` and
  `scripts/moderation_eval.py:108` stamp a model artifact version
  (`domain-mod-{date:%Y-%m-%d}`) → `datetime.now(timezone.utc).date()`, so a retrain and
  a warm start started either side of local midnight cannot disagree
- `test/test_search_api_key.py:598`, `:615`, `:635`, `:654` derive the year and month of
  a `UsageBucket` row → `datetime.now(timezone.utc)`, matching what
  `mwmbl/background.py:121` already does in `sync_search_counts`
- `analyse/update_urls.py:18`, `:20` (timing) and `analyse/wiki_stats.py:30`, `:31`
  (sampling hours from the last month) → `datetime.now(timezone.utc)` and
  `datetime(..., tzinfo=timezone.utc)`. These are analysis scripts, not application code;
  keep the change to the call itself.
- `pyproject.toml`: `select = ["E", "F", "I"]` → `select = ["DTZ", "E", "F", "I"]`
- `notes/lint-and-typecheck-backlog.md`: delete the `DTZ` entry from Tier 1, and note in
  "How the checks work today" that `DTZ` is now in `select`
- Delete `docs/plans/issue-371-enable-dtz-rule.md`

- `uv run ruff check --no-cache --select DTZ --statistics` reports zero
- `make check` passes with `DTZ` in `select`, and fails if a `datetime.utcnow()` is added
  back to any file
- `make test` passes
