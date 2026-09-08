# Working on Mwmbl

Mwmbl is a non-profit, open source web search engine: a Django application serving search
and curation, plus a distributed crawler run by volunteers. This file is the context a
coding agent gets; see `README.md` for the project itself and
[book.mwmbl.org](https://book.mwmbl.org) for the full developer guide.

## Commands

Use `uv` for everything — never a bare `python` or `pip`.

```sh
make check                  # ruff format --check, ruff check, ty — exactly what CI gates on
make fix                    # ruff format + ruff check --fix
make test                   # the full pytest suite
make test-file FILE=test/test_auth.py
uv run manage.py <command> --settings=mwmbl.settings_dev
```

`make test` and `make migrate` need `DATABASE_URL` in the environment, for example
`DATABASE_URL="postgres://user@/mwmbl_test"`. Tests use `mwmbl.settings_test`.

`make check` and `make test` are the two gates. Both must pass before you open a pull
request, and `.pre-commit-config.yaml` runs the first of them on every commit.

## Layout

- `mwmbl/` — the Django project. `crawler/` (crawl queue and batches), `indexer/` (the
  index and its update pipeline), `moderation/`, `platform/`, `evaluation/` and
  `rankeval/` (ranking quality), `api.py` (django-ninja endpoints), `models.py`,
  `settings_*.py` per environment.
- `mwmbl_rank/` — the Rust ranking extension, built with maturin. Changing it means
  `uv run maturin develop` before the tests will see it.
- `test/` — pytest suite, one file per area.
- `front-end/` — the JS build; leave it alone unless the change is about the UI.
- `scripts/`, `analyse/` — one-off analysis and evaluation tools, not application code.
- `devdata/` — generated data artifacts, mostly gitignored. Never commit large files here.
- `docs/plans/` — implementation plans for issues being built one pull request at a time;
  see `docs/plans/README.md`.

## Style

- No defensive programming. Let it fail loudly rather than papering over a missing
  setting or an unexpected shape — no `getattr(settings, "X", default)`, no bare
  `except`, no "just in case" fallbacks.
- Name intermediate results instead of nesting expressions. A well-named local is the
  cheapest documentation there is.
- Imports go at the top of the file.
- Comments explain *why*, not *what*. Skip the comment that restates the line below it.
- Match the surrounding code's naming and idiom over any general preference.
- Tests live next to the behaviour they cover, in `test/`, and are part of the same
  change — not a follow-up.

## Pull requests

Keep them small: at most about 500 added lines, tests included. Say what changed, why,
and how it was verified. Reference the issue with `Part of #<N>`, or `Closes #<N>` when
it finishes the issue.
