# Issue #557: Extract a pure-Python `mwmbl-crawler` package installable with `uv tool install`

Steps 1-4 of the issue are done or in flight: the version-spread dashboard (#558 + mwmbl/front-end#99), single-source `CRAWLER_VERSION` with tagged `crawler-vX.Y.Z` releases, `MIN_CRAWLER_VERSION` + 426 (#573), and the systemd auto-update timer for Docker users (#574 + mwmbl/book#32). What remains is step 5: the crawler only ships as a Docker image because `mwmbl/crawl.py` calls `django.setup()` and its import graph reaches Django settings, the Django cache and the Rust extension. When this plan is finished, a volunteer runs `uv tool install mwmbl-crawler`, gets a `mwmbl-crawl` command from a `py3-none-any` (or small per-platform wheel) package on PyPI, and the Docker image is a thin `pip install mwmbl-crawler==X` over the same release.

Coupling found by reading the graph loaded by `import mwmbl.crawl` (the same set `mwmbl/crawler/release_check.py` enumerates):

- `django.conf.settings` for config: `crawl.py`, `mwmbl/utils.py` (REQUEST_CACHE_PATH), `mwmbl/crawler/urls.py` and `domains.py` (bloom filter paths), `mwmbl/indexer/blacklist_snapshot.py` (REDIS_URL, refresh interval), `mwmbl/redis_url_queue.py` (`CORE_DOMAINS` from settings), `mwmbl/tinysearchengine/rank.py` (BLACKLIST_FILTER_AT_RETRIEVAL).
- `django.core.exceptions.ValidationError` in `mwmbl/utils.py:get_domain`; `django.core.cache` in `rank.py` (Wikipedia circuit breaker, Ranker paths only - the crawler needs just `score_result`/`get_features`).
- `mwmbl.apps` (AppConfig + `create_index`) imported by `crawl.py`.
- The Rust extension: `mwmbl/tinysearchengine/indexer.py` imports `pack_index_page`/`read_index_page`/`write_index_page` from `mwmbl_rank`, whose `.so` links `libxgboost`. The crawler needs the index; it does not need xgboost.
- Already clean: `mwmbl/indexer/update_urls.py` ("database" there means the bloom-filter URL database, not Postgres), `mwmbl/indexer/agreement.py`, `mwmbl/tokenizer.py`, `mwmbl/justext`, the server-only modules `mwmbl/crawler/app.py`/`stats.py`/`agreement.py`/`worst_agreements.py` are not in the crawler's graph.

## PR 1: Crawler-local config, no `django.conf` in the crawler graph
**Estimated added lines:** ~250
**Files:** new `mwmbl/crawler/config.py`, `mwmbl/crawl.py`, `mwmbl/utils.py`, `mwmbl/crawler/urls.py`, `mwmbl/crawler/domains.py`, `mwmbl/indexer/blacklist_snapshot.py`, `mwmbl/redis_url_queue.py`, `mwmbl/tinysearchengine/rank.py`, `test/test_crawler_no_django.py`

- New `mwmbl/crawler/config.py`: reads every value the crawler graph needs from environment variables with the same defaults as `settings_crawler.py` (DATA_PATH, INDEX_NAME, NUM_PAGES, bloom filter paths, REDIS_URL, REQUEST_CACHE_PATH, BLACKLIST_SNAPSHOT_*, BLACKLIST_FILTER_AT_RETRIEVAL, CORE_DOMAINS moved out of `settings.py` into a shared data module both sides import). No Django import.
- Point the modules above at `config` instead of `django.conf.settings`. Server-side users of the same modules keep working because the server sets the same values through Django settings and the crawler config is only read on the crawler path — where that is not true, pass the value as a parameter instead.
- `mwmbl/utils.py`: drop the `ValidationError` (raise `ValueError`; fix server callers that catch `ValidationError` from `get_domain`), and make `request_cache`/`prune_request_cache` take the cache path from config or a parameter.
- `rank.py`: split so `score_result`/`get_features` and the crawler path do not import `django.core.cache` or the external-cache module; the Wikipedia circuit breaker stays in the server-side Ranker class.
- Acceptance: a new `test/test_crawler_no_django.py` imports `mwmbl.crawl` in a child process with `DJANGO_SETTINGS_MODULE` unset and asserts no `django.*` module except an explicit whitelist is loaded; `make check` and the crawler tests pass; the server still passes its suite unchanged.

## PR 2: Split the index page codec out of `mwmbl_rank` into its own extension
**Estimated added lines:** ~300 (Rust moved, not rewritten)
**Files:** `mwmbl_rank/src/index.rs` (moved), `mwmbl_rank/Cargo.toml` (split), new crate `mwmbl_index/`, `mwmbl/tinysearchengine/indexer.py`, `pyproject.toml`, CI build of the new wheels

- Move `index.rs` (pack/read/write_index_page and its helpers) into a new `mwmbl_index` PyO3 crate with no xgboost dependency; `mwmbl_rank` keeps the ranking/LTR code and depends on `mwmbl_index` (or duplicates nothing - one owner of the page format).
- `tinysearchengine/indexer.py` imports the codec from `mwmbl_index`. The server links the same codec, so the index format has exactly one implementation still.
- Build and publish per-platform wheels for `mwmbl_index` (x86_64/aarch64 linux + macOS) in CI; maturin makes this a small job. The crawler package then depends on `mwmbl-index`, not `mwmbl-rank`.
- Acceptance: byte-identical pages — write with old, read with new and vice versa, asserted in a test over a fixture index; full test suite passes; `uv run maturin develop` workflow updated in AGENTS.md if the build changes.

## PR 3: `import mwmbl.crawl` needs no Django at all
**Estimated added lines:** ~150
**Files:** `mwmbl/crawl.py`, `mwmbl/apps.py`, delete `mwmbl/settings_crawler.py`, `mwmbl/crawler/release_check.py`, `test/test_crawl_import_order.py`, CI

- Remove `django.setup()` and the `DJANGO_SETTINGS_MODULE` line from `crawl.py`; move `create_index` out of `mwmbl/apps.py` into a Django-free module the server also calls from its AppConfig.
- Delete `settings_crawler.py`; the crawler is configured purely by env vars via `config.py`.
- `release_check.py` still imports `mwmbl.crawl` to enumerate crawler modules — keep that working with no Django installed (it already runs in a child process; assert it now also runs with no Django on the path, e.g. a venv without Django).
- Acceptance: `test_crawl_import_order` and the new no-Django test pass; CI crawler smoke job runs the image built from a base without Django; release_check passes.

## PR 4: Move the crawler into `packages/crawler` as a uv workspace member
**Estimated added lines:** ~400 (mostly moves)
**Files:** `packages/crawler/` (pyproject `mwmbl-crawler`, `src/mwmbl_crawler/` or a flat layout matching today's modules), `pyproject.toml` (workspace), `Dockerfile.crawler`, `mwmbl/crawler/release_check.py`, `test/`, book

- uv workspace: root `pyproject.toml` gains `packages/crawler` as a member. The crawler package contains exactly the modules PR 1-3 put in its graph (crawl entry point, crawler/, the indexer pieces it uses, tinysearchengine index/tokenizer/rank-score subset, justext, utils subset, redis_url_queue) with entry point `mwmbl-crawl = ...:main`.
- The server package keeps what it needs; small pure helpers shared by both (tokenizer, page codec client, schemas in `crawler/batch.py`) are imported from the installed `mwmbl-crawler` package or duplicated deliberately - pick one and document it, so the two cannot drift (the issue's reason for keeping one repo).
- `release_check.py` learns the new paths: crawler code = the workspace member + `mwmbl_rank` codec crate; `CRAWLER_VERSION` stays the single source.
- Acceptance: `uv tool install ./packages/crawler` (or from a built wheel) gives a working `mwmbl-crawl --help`; crawler smoke CI runs against the installed package; server tests pass; Dockerfile.crawler still builds.

## PR 5: Publish to PyPI, thin Docker image, `uv tool` systemd units, book rewrite
**Estimated added lines:** ~250
**Files:** `.github/workflows/crawler-package-release.yml`, `Dockerfile.crawler`, `deploy/crawler/` (new uv-based units), book quick start, AGENTS.md

- On a `crawler-vX.Y.Z` tag, CI builds the `mwmbl-crawler` wheel(s) + `mwmbl-index` wheels and publishes to PyPI with trusted publishing (OIDC) - no long-lived token; Docker Hub publish stays for the image.
- `Dockerfile.crawler` becomes `FROM python:3.11-slim` + `pip install mwmbl-crawler==<version>` (version baked at build from the tag), so Docker and non-Docker volunteers run the same release bits.
- New `deploy/crawler/mwmbl-crawler-uv-updater.{service,timer}` running `uv tool upgrade mwmbl-crawler` hourly with `RandomizedDelaySec=1h` for non-Docker installs; the existing compose timer stays for Docker users.
- Book: quick start becomes `uv tool install mwmbl-crawler` + `mwmbl-crawl` with the systemd timer; Docker Compose remains as the alternative. Delete this plan file and say `Closes #557`.
