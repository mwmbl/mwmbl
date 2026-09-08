#!/usr/bin/env bash
#
# Run a command, and run it again if it fails.
#
# For build steps whose failures are dominated by the network rather than by the code:
# see the comment in .github/actions/setup-project/action.yml for the one that made this
# necessary. A command that fails because the tree is broken fails every attempt, so the
# only thing this costs in that case is the time of the extra attempts.

set -euo pipefail

attempts=${RETRY_ATTEMPTS:-3}
delay=${RETRY_DELAY:-15}

for attempt in $(seq 1 "$attempts"); do
  status=0
  "$@" || status=$?
  if [[ $status -eq 0 ]]; then
    exit 0
  fi
  if [[ $attempt -eq $attempts ]]; then
    echo "::error::'$*' failed $attempts times; giving up."
    exit "$status"
  fi
  echo "::warning::'$*' failed (attempt $attempt of $attempts, exit $status); retrying in ${delay}s."
  sleep "$delay"
done
