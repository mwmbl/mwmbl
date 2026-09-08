#!/usr/bin/env bash
#
# Turn the run log into a job result, distinguishing the two ways a Claude step fails.
#
# A step whose result reports is_error after a single turn with no model usage never
# reached the model at all: the subscription's usage window is spent, or the token has
# expired. That is not a broken change and it needs nobody's attention — the queues are
# pure functions of repository state, so the item is still eligible and the next scheduled
# run picks it up untouched. The job stays green, because a spent usage window reading as
# a code failure is what makes these runs impossible to watch.
#
# Any other failure belongs to this run. Repeating it every hour would spend the budget on
# the same mistake, so the issue is labelled "claude: stuck" and the selector skips it
# until a human removes the label or reruns that issue alone with workflow_dispatch.

set -euo pipefail

log="${CLAUDE_RUN_LOG:?}"
issue="${ISSUE_NUMBER:?}"
readonly stuck_label="claude: stuck"

readonly never_reached_model='
    .result.is_error == true
    and .result.num_turns <= 1
    and ((.result.modelUsage // {}) | length) == 0
'

steps_matching() {
    jq -r "select(.outcome == \"failure\") | select($1) | .step" "$log" | paste -sd' ' -
}

broken=$(steps_matching "($never_reached_model) | not")
out_of_credit=$(steps_matching "$never_reached_model")

if [[ -n $broken ]]; then
    echo "::error::Claude failed in: $broken. Labelling issue #$issue '$stuck_label' so the" \
        "scheduled runs stop retrying it; remove the label, or rerun this issue alone with" \
        "workflow_dispatch, once the cause is understood."
    gh issue edit "$issue" --add-label "$stuck_label"
    exit 1
fi

if [[ -n $out_of_credit ]]; then
    echo "::notice::$out_of_credit never reached the model — the subscription's usage window" \
        "is spent, or CLAUDE_CODE_OAUTH_TOKEN has expired. Issue #$issue is unchanged and" \
        "stays queued for the next run."
    exit 0
fi

# A clean run clears the label, so a human who reran a stuck issue by hand does not have to
# remember to take it off afterwards.
if gh issue view "$issue" --json labels --jq '.labels[].name' | grep -qxF "$stuck_label"; then
    gh issue edit "$issue" --remove-label "$stuck_label"
fi
