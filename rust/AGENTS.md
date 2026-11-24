# Writing Rust in Mwmbl

These rules apply to every Rust crate in the repository: `rust/mwmbl_core`, `rust/crawler`
and `mwmbl_rank`. They describe the style of the hand-written code in `mwmbl_core` and the
crawler; when in doubt, read `rust/mwmbl_core/src/blacklist_providers.rs` or
`rust/crawler/src/url_queue.rs` and match them. Some patterns in those files predate these
rules (an `Arc` or a `.clone()` that isn't needed, for example): follow the rules in new
code rather than copying them.

## Lints

Every crate's root module starts with this block, and `cargo clippy --all-targets -- -D
warnings` must pass. (`mwmbl_rank` doesn't have it yet; it is being brought into line.)

```rust
#![cfg_attr(
    not(test),
    deny(
        clippy::exit,
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::unwrap_used,
        clippy::panic,
        clippy::string_slice,
        clippy::unimplemented,
        clippy::unreachable,
        clippy::todo,
        clippy::pedantic
    )
)]
```

`mwmbl_core` still allows `clippy::missing_errors_doc` crate-wide, because keeping those
docs up to date by hand is costly. Code written by agents doesn't need that exception:
document the errors (see below). Fix the code rather than adding `#[allow(...)]`. An `allow` needs a reason a reviewer would accept, such as a cast
whose range is known.

## Errors

The root `AGENTS.md` says to fail loudly. In Rust that means returning an `Err`, never
panicking.

- Return `anyhow::Result`. Fail with `bail!`, and add context with `.context()`. Don't use
  `Result<_, String>` or build error messages with `.map_err(|e| format!(...))`.
- Convert to another error type only at a boundary, such as a Python exception in a pyo3
  binding.
- Never discard a `Result` with `let _ =`, and never return `Ok(())` from a path that
  failed. If the caller should carry on after an error, log it with `eprintln!` and say
  what failed.
- Don't turn bad input into a default (`.unwrap_or_default()` on a parse or an
  extraction). Return the error.

## Types and ownership

- Prefer generics and `impl Trait` to `Box<dyn Trait>` and `Box::pin`. See
  `BlacklistProvider`, whose methods return `impl Future`, and `Crawler<T:
  BlacklistProvider = DefaultBlackList>`, whose default type parameter keeps the generic
  out of callers' way.
- Add `Arc`, `Mutex` or `.clone()` only when ownership requires it. Check whether the type
  is already `Clone`, `Send` or `Sync` before wrapping it.
- Use `.to_owned()`, not `.to_string()`, to turn a `&str` into a `String`.
- Borrow where you can: take `&str` and `&[T]` rather than `String` and `Vec<T>` when the
  function doesn't keep them.
- At a boundary, parse into a typed struct once (`serde`, or `#[derive(FromPyObject)]` in
  pyo3) instead of reading fields one by one.

## Control flow

- Exit early with `let ... else` rather than nesting the rest of the function inside
  `if let`:

  ```rust
  let Some(domain) = url.domain() else {
      return (claimed_url, CrawlResult::NoDomainError { timestamp, url });
  };
  ```

  On a `Result`, the `else` arm can't see the error, so use it only where the error has
  already been reported; otherwise `match` and log or return it.

- Use iterator chains for pure transformations. Use a `for` loop when the body has side
  effects (pushing to another collection, logging) or awaits.
- Don't index or slice by computed positions (`v[i]`, `&flat[i * w..(i + 1) * w]`).
  Iterate with `zip`, `chunks_exact`, `windows` or `enumerate`, or use `.get()` and handle
  `None`.
- Keep functions under Clippy's 100-line limit, and split one that does several separable
  things even when it is shorter.

## Comments and output

- Comment sparingly. Names should carry the meaning; most of the hand-written code has
  almost no comments.
- A doc comment on a public item says what it does, briefly. Explain why only when the
  reason isn't visible in the code.
- A public function that returns a `Result` has an `# Errors` section saying when it
  fails:

  ```rust
  /// Reads the page at `offset`.
  ///
  /// # Errors
  ///
  /// Fails if the read comes up short or the page doesn't decode.
  ```

  When you change such a function, update its `# Errors` section to match.
- Don't narrate history ("this used to...", "was written as..."). That belongs in the
  commit message.
- No timing or progress prints in library code. Print only in binaries, or to report an
  error that is being handled.

## Dependencies and edition

- Use edition 2024.
- Prefer the standard library to a crate: `std::sync::LazyLock` rather than `once_cell` or
  `lazy_static`.
- Remove dependencies that are no longer used.
