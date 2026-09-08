#!/usr/bin/env bash
#
# Append one Claude step's outcome and its result message to $CLAUDE_RUN_LOG.
#
# claude-code-action writes the step's messages to a path under $RUNNER_TEMP; the log this
# builds pulls out just the result, which is what tells a step that never reached the model
# from one where Claude ran and genuinely failed. See classify-claude-run.sh.
#
# $OUTCOME and $EXECUTION_FILE come from the step being recorded, as steps.<id>.outcome and
# steps.<id>.outputs.execution_file.

set -euo pipefail

step="$1"
log="${CLAUDE_RUN_LOG:?}"

# A step that was skipped, or one that died before Claude produced anything, leaves no
# execution file. An empty result is how the classifier sees "nothing to go on".
if [[ -f ${EXECUTION_FILE:-} ]]; then
    result=$(jq -c 'map(select(.type == "result")) | last // {}' "$EXECUTION_FILE")
else
    result='{}'
fi

jq -nc --arg step "$step" --arg outcome "$OUTCOME" --argjson result "$result" \
    '{step: $step, outcome: $outcome, result: $result}' | tee -a "$log"
