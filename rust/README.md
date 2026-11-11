# Rust crates

- `mwmbl_core`: code shared between the server and the Rust crawler. With default features it
  holds only the index page format (`index.rs`), which `mwmbl_rank` links into the server's
  Python extension. A change to it needs a crawler release, see `mwmbl/crawler/release_check.py`.
- `mwmbl_core` with the `crawler` feature, and `crawler`: the Rust crawler. **Work in
  progress:** nothing builds it into an image or runs it yet, and it isn't released.

`make rust-core` formats, lints and tests them. CI requires clippy to be clean for
`mwmbl_core`'s default features only; the crawler code only has to build and pass its tests.
