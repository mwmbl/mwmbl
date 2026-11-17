#!/bin/sh
# Find the best CRAWLER_WORKERS and CRAWL_THREADS for a Docker Compose crawler on this machine.
# Needs only what running the crawler needs, plus python3 (3.9 or later), which Linux and macOS
# ship. Run it in the directory you start your crawler from:
#
#   ./deploy/crawler/benchmark.sh                  (in a checkout: runs the benchmark.py beside it)
#   curl -fsSL https://raw.githubusercontent.com/mwmbl/mwmbl/main/deploy/crawler/benchmark.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/mwmbl/mwmbl/main/deploy/crawler/benchmark.sh | sh -s -- --processes 1,2,4
#
# It stops your crawler, tries settings on it for a few seconds each, and starts it again as it was.
set -eu

BENCHMARK_URL="${MWMBL_BENCHMARK_URL:-https://raw.githubusercontent.com/mwmbl/mwmbl/main/mwmbl/crawler/benchmark.py}"

fail() {
    echo "benchmark: $*" >&2
    exit 1
}

# Everything is in a function called on the last line, so a download cut short runs nothing.
main() {
    command -v python3 >/dev/null 2>&1 || fail "needs python3 (3.9 or later); install it with your package manager."
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "needs python3 3.9 or later."
    command -v docker >/dev/null 2>&1 || fail "needs docker, which runs the crawler."
    docker compose version >/dev/null 2>&1 || fail "needs 'docker compose' (the Compose v2 plugin)."
    if ! docker_error="$(docker info 2>&1 >/dev/null)"; then
        case "$docker_error" in
        *"permission denied"*)
            fail "your user cannot use docker (permission denied). Add it to the docker group with" \
                "'sudo usermod -aG docker \$USER', then log out and back in (or run 'newgrp docker')." ;;
        *) fail "cannot talk to the docker daemon. Is it running? docker said: $docker_error" ;;
        esac
    fi

    # Piped from curl, $0 is the shell and not a file. In a checkout the file beside this one is
    # the version that goes with it, where the one on main may be older.
    if [ -f "$0" ] && [ -f "$(dirname -- "$0")/../../mwmbl/crawler/benchmark.py" ]; then
        exec python3 "$(dirname -- "$0")/../../mwmbl/crawler/benchmark.py" "$@"
    fi

    command -v curl >/dev/null 2>&1 || fail "needs curl."
    script="$(mktemp "${TMPDIR:-/tmp}/mwmbl-benchmark.XXXXXX")"
    trap 'rm -f "$script"' EXIT
    trap 'exit 130' INT TERM
    curl -fsSL "$BENCHMARK_URL" -o "$script" || fail "could not download $BENCHMARK_URL"

    python3 "$script" "$@"
}

main "$@"
