#!/bin/sh
# Find the best CRAWLER_WORKERS and CRAWL_THREADS for a Docker Compose crawler on this machine.
# Needs only what running the crawler needs, plus python3 (3.9 or later), which Linux and macOS
# ship. Run it in the directory you start your crawler from:
#
#   curl -fsSL https://raw.githubusercontent.com/mwmbl/mwmbl/main/deploy/crawler/benchmark.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/mwmbl/mwmbl/main/deploy/crawler/benchmark.sh | sh -s -- --exhaustive
#
# It stops your crawler, tries settings on it for a few seconds each, and starts it again as it was.
set -eu

BENCHMARK_URL="${MWMBL_BENCHMARK_URL:-https://raw.githubusercontent.com/mwmbl/mwmbl/main/mwmbl/crawler/benchmark.py}"

fail() {
    echo "benchmark: $*" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || fail "needs python3 (3.9 or later); install it with your package manager."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "needs python3 3.9 or later, found $(python3 --version)."
command -v docker >/dev/null 2>&1 || fail "needs docker, which runs the crawler."
docker compose version >/dev/null 2>&1 || fail "needs 'docker compose' (the Compose v2 plugin)."
command -v curl >/dev/null 2>&1 || fail "needs curl."

script="$(mktemp "${TMPDIR:-/tmp}/mwmbl-benchmark.XXXXXX")"
trap 'rm -f "$script"' EXIT INT TERM
curl -fsSL "$BENCHMARK_URL" -o "$script" || fail "could not download $BENCHMARK_URL"

python3 "$script" "$@"
