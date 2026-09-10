# Mwmbl development Makefile
#
# Prerequisites:
#   - uv (https://github.com/astral-sh/uv)
#   - PostgreSQL running locally with peer auth for the current user
#   - Redis running locally on the default port (6379)
#
# Quick start:
#   createdb mwmbl          # one-time setup for dev DB
#   createdb mwmbl_test     # one-time setup for test DB
#   make migrate            # apply all migrations
#   make test               # run the full test suite
#   make run                # start the dev server

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Override these via environment or make invocation, e.g.:
#   DATABASE_URL="postgres://user:pass@host/db" make test
# These have no defaults — you must set them in your environment or on the command line.
DATABASE_URL   ?=
REDIS_URL      ?= redis://127.0.0.1:6379

DJANGO_SETTINGS_MODULE ?= mwmbl.settings_dev
TEST_SETTINGS          := mwmbl.settings_test

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

SODIR := .venv/lib/python3.11/site-packages/mwmbl_rank
XGB_SO := mwmbl_rank/target/release/deps/libxgboost.so

.PHONY: help install install-hooks patch-xgboost migrate test test-file run run-background \
        check fix format format-check lint lint-fix typecheck typecheck-all locked

help:
	@echo "Available targets:"
	@echo "  install        Install all dependencies with uv"
	@echo "  migrate        Apply Django migrations"
	@echo "  test           Run the full test suite (set DATABASE_URL to your test DB)"
	@echo "  test-file FILE Run a specific test file, e.g. make test-file FILE=test/test_search_api_key.py"
	@echo "  run            Start the Django development server"
	@echo "  run-background Start the background task processor"
	@echo ""
	@echo "  check          Run every check CI runs: locked, format-check, lint, typecheck"
	@echo "  fix            Auto-fix what can be auto-fixed: format + lint --fix"
	@echo "  format         Reformat the code with ruff"
	@echo "  format-check   Check formatting without writing (CI gate)"
	@echo "  lint           Run ruff lint checks"
	@echo "  lint-fix       Run ruff lint checks, applying safe fixes"
	@echo "  typecheck      Run ty, failing only on error-level diagnostics (CI gate)"
	@echo "  typecheck-all  Run ty showing the full warning backlog, never fails"
	@echo "  locked         Check uv.lock is up to date with pyproject.toml (CI gate)"
	@echo "  install-hooks  Install the pre-commit hooks into .git/hooks"

install:
	uv sync
	$(MAKE) patch-xgboost

# Copy libxgboost.so into the mwmbl_rank package dir and set RPATH to $ORIGIN,
# matching the Dockerfile post-build step so the extension is self-contained.
patch-xgboost:
	@if [ -f "$(XGB_SO)" ]; then \
		cp "$(XGB_SO)" "$(SODIR)/"; \
		.venv/bin/patchelf --set-rpath '$$ORIGIN' "$(SODIR)/mwmbl_rank.cpython-311-x86_64-linux-gnu.so"; \
		echo "Patched RPATH and copied libxgboost.so to $(SODIR)"; \
	else \
		echo "$(XGB_SO) not found — run 'uv run maturin develop' first"; \
	fi

migrate:
	DATABASE_URL="$(DATABASE_URL)" REDIS_URL="$(REDIS_URL)" \
		uv run python manage.py migrate --settings=$(DJANGO_SETTINGS_MODULE)

test:
	DATABASE_URL="$(DATABASE_URL)" REDIS_URL="$(REDIS_URL)" \
		uv run pytest $(PYTEST_ARGS)

test-file:
	DATABASE_URL="$(DATABASE_URL)" REDIS_URL="$(REDIS_URL)" \
		uv run pytest $(FILE) -v $(PYTEST_ARGS)

run:
	DATABASE_URL="$(DATABASE_URL)" REDIS_URL="$(REDIS_URL)" \
		uv run python manage.py runserver --settings=$(DJANGO_SETTINGS_MODULE)

run-background:
	DATABASE_URL="$(DATABASE_URL)" REDIS_URL="$(REDIS_URL)" \
		uv run python manage.py process_tasks --settings=$(DJANGO_SETTINGS_MODULE)

# ---------------------------------------------------------------------------
# Checks
#
# `make check` is exactly what CI and the pre-commit hook run. Keep the three
# targets below in sync with .pre-commit-config.yaml and .github/workflows/ci.yml.
# ---------------------------------------------------------------------------

# `locked` comes first on purpose: every other target below shells out to `uv run`,
# which relocks uv.lock in place when pyproject.toml has moved on, and a lockfile that
# has just been rewritten always passes `uv lock --check`.
check: locked format-check lint typecheck

fix: format lint-fix

format:
	uv run ruff format

format-check:
	uv run ruff format --check

lint:
	uv run ruff check

lint-fix:
	uv run ruff check --fix

# Most rules are downgraded to "warn" in pyproject.toml (see [tool.ty.rules]) because
# they have a large pre-existing baseline - notably Django's `Model.objects`, which ty
# cannot see. Print the error-level diagnostics only, and fail on those alone, so the
# warning backlog does not bury a real regression.
typecheck:
	@output=$$(uv run ty check --exit-zero-on-warning --output-format concise 2>&1); \
		status=$$?; \
		echo "$$output" | grep -E ' error\[' || true; \
		echo "$$output" | tail -n 1; \
		exit $$status

# The full backlog, warnings included. Advisory: never fails the build.
typecheck-all:
	uv run ty check --exit-zero-on-warning

# The Docker images install with `uv sync --frozen`, which takes uv.lock exactly as
# committed, so a pyproject.toml edited without relocking would leave them building the
# old dependency set while CI and the tests run the new one.
locked:
	uv lock --check

install-hooks:
	uv run pre-commit install
