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

.PHONY: help install patch-xgboost migrate test test-file run run-background lint

help:
	@echo "Available targets:"
	@echo "  install        Install all dependencies with uv"
	@echo "  migrate        Apply Django migrations"
	@echo "  test           Run the full test suite (set DATABASE_URL to your test DB)"
	@echo "  test-file FILE Run a specific test file, e.g. make test-file FILE=test/test_search_api_key.py"
	@echo "  run            Start the Django development server"
	@echo "  run-background Start the background task processor"

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
