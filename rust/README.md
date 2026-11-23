# Rust crates

- `mwmbl_core`: code shared between the server and the crawler, which `mwmbl_rank` links into
  the server's Python extension. A change to it needs a crawler release, see
  `mwmbl/crawler/release_check.py`.
- `crawler`: the crawler volunteers run, see [its README](crawler/README.md).

`make rust-core` formats, lints and tests them, and CI requires both to be clippy-clean.

`make optimize-rust` (`python3 -m mwmbl.crawler.benchmark_rust`) finds the best `CRAWL_THREADS`,
`BLOCKING_THREADS` and `CRAWL_MAX_TASKS` for the crawler on a machine. It runs the release build
as a child process, so stop a Compose crawler first, and counts the pages the crawler prints as
it crawls them. See the module docstring for what it measures.
