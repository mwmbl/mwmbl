#!/usr/bin/env bash
#
# Append one Claude step's outcome and its result message to $CLAUDE_RUN_LOG, and leave
# its final message where a later step can read it.
#
# claude-code-action writes every step's messages to the same path under $RUNNER_TEMP and
# overwrites it, so a step's result has to be copied out before the next step runs. The
# log this builds is what tells a step that never reached the model from one where Claude
# ran and genuinely failed; see classify-claude-run.sh.
#
# The final message is copied out for a second reason. Each step is a fresh session that
# can see nothing of the one before it, and the finalise step is asked to write a pull
# request body describing what the build and the review did — which it can only do if it
# is handed what they said. Its prompt points at these files.
#
# $OUTCOME and $EXECUTION_FILE come from the step being recorded, as
# steps.<id>.outcome and steps.<id>.outputs.execution_file.

set -euo pipefail

step="$1"
log="${CLAUDE_RUN_LOG:?}"
summary_dir="${CLAUDE_SUMMARY_DIR:?}"

# A step that was skipped, or one that died before Claude produced anything, leaves no
# execution file. An empty result is how the classifier sees "nothing to go on".
if [[ -f ${EXECUTION_FILE:-} ]]; then
    result=$(jq -c 'map(select(.type == "result")) | last // {}' "$EXECUTION_FILE")
else
    result='{}'
fi

jq -nc --arg step "$step" --arg outcome "$OUTCOME" --argjson result "$result" \
    '{step: $step, outcome: $outcome, result: $result}' | tee -a "$log"

# An empty file rather than a missing one: the step that reads it should see that this
# step reported nothing, not have to guess whether the path is right.
mkdir -p "$summary_dir"
jq -r '.result // ""' <<<"$result" >"$summary_dir/$step.md"
