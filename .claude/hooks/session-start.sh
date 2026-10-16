#!/bin/bash
# Claude Code on the web: install dependencies and build the Rust extension.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# xgboost_lib-sys's build script downloads a prebuilt libxgboost.so from
# github.com/marcomq/rust-xgboost. The web session's GitHub proxy only serves the
# session's own repositories, so the download comes back as a JSON error, which the
# build script writes out as the library and linking then fails with "unknown file
# type". The script skips the download when target/<profile>/deps already holds the
# library, so seed it there from the xgboost-cpu wheel, pointing its RPATH at the
# wheel's bundled libgomp.
uv sync --group dev --no-install-project
site_packages="$PWD/.venv/lib/python3.11/site-packages"
for profile in debug release; do
  deps="mwmbl_rank/target/$profile/deps"
  mkdir -p "$deps"
  cp "$site_packages/xgboost/lib/libxgboost.so" "$deps/"
  .venv/bin/patchelf --set-rpath "$site_packages/xgboost_cpu.libs" "$deps/libxgboost.so"
done

uv sync --group dev
