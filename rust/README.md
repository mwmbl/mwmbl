# Rust crates

- `mwmbl_core`: code shared between the server and the crawler, which `mwmbl_rank` links into
  the server's Python extension. A change to it needs a crawler release, see
  `mwmbl/crawler/release_check.py`.
- `crawler`: the crawler volunteers run, see [its README](crawler/README.md).

`make rust-core` formats, lints and tests them, and CI requires both to be clippy-clean.
