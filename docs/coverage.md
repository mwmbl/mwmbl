# Code coverage

## Run it locally

Install the Rust coverage tool once:

```sh
rustup component add llvm-tools-preview
cargo install cargo-llvm-cov --locked
```

If Cargo reports that its binary directory is not on `PATH`, add
`$HOME/.cargo/bin`; the Make target also checks that standard location.

Then run:

```sh
DATABASE_URL="postgres://user@/mwmbl_test" make coverage
```

This writes:

- `coverage/python.xml` and `coverage/python-html/` for Python;
- `coverage/rust.lcov` for Rust; and
- Python, Rust, and combined totals to the terminal.

The combined percentage is `(covered Python lines + covered Rust lines) /
(executable Python lines + executable Rust lines)`. Weighting by executable
lines prevents a small language component from counting as much as a large one.
Python and Rust results are also printed separately because the combined number
must not hide a weak component.

The Rust target runs both native unit tests and the Python integration tests
against an instrumented PyO3 extension. Measuring only `cargo test` would miss
the extension boundary even though the Python suite exercises it. The Python tests
that import `mwmbl_rank` are listed in `RUST_COVERAGE_TESTS` in the Makefile; add a
test file there when it is the only thing exercising a Rust path. The Make target
uses `uv run --no-sync` because a plain `uv run` silently rebuilds the extension
without instrumentation, which makes `index.rs` and `lib.rs` read as 0%.

If the Python suite fails on background-task or dedupe tests with surprising row
counts, the reused test database (`--reuse-db`) is stale; run once with
`PYTEST_ARGS=--create-db`.

## What counts

Coverage should measure behavior that can regress. Migrations are generated
history, ASGI/WSGI modules are framework launchers, and `indexer/paths.py` is
declarative machine-specific path configuration, so they are omitted.
Type-check-only blocks and abstract method bodies are excluded as structurally
unreachable code.

Constructors, properties, serializers, database helpers, management commands,
and index tools are not excluded just because they are short or inconvenient to
test. They contain behavior and should remain visible in the report. Empty
constructors need no special pattern: coverage.py only counts executable
statements, not a function declaration with no body.

The useful ceiling is reached when the remaining misses are failure handling
that cannot be triggered deterministically or environment-specific integration
paths. For this project, aim for at least 85% in each language before considering
further exclusions. Prioritize uncovered decisions in API permissions, views,
ranking, crawling, moderation, and external-source adapters over tests that only
call trivial accessors.

The baseline when this workflow was introduced was 70.83% Python, 66.36% Rust,
and 70.42% combined; including the extension-boundary tests lifts Rust to 91.6% and
the combined figure to 72.7%. The largest actionable Python gaps were `platform/api.py`,
`views.py`, moderation training, crawler retrieval, and crawler application
behavior. Standalone ranking-evaluation reports also account for many misses;
test maintained reports through their parsing and scoring functions, and delete
obsolete reports rather than hiding them from coverage.

## CI and badges

GitHub Actions uploads both reports to Codecov. Codecov combines them and drives
the badge in the main README.

The GitLab coverage job prints a line such as `TOTAL COVERAGE: 82.14%`; its
`coverage` expression makes that value available to GitLab's built-in badge.
For a mirror at `https://gitlab.com/<namespace>/<project>`, the badge URL is:

```text
https://gitlab.com/<namespace>/<project>/badges/main/coverage.svg
```

Replace the namespace and project with the mirror's actual path. This repository
currently has no GitLab remote, so committing a concrete GitLab badge URL would
create a broken badge.
