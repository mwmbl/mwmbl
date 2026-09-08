#!/usr/bin/env bash
#
# Pick the issues that the Claude issue-automation workflow should act on.
#
# The queues are derived entirely from repository and issue state, never from a label
# the automation itself has to remember to change, so a rerun after a failed job is
# harmless and a human can redirect the work by editing a plan file:
#
#   implement  the issue has a plan under docs/plans/ with a section not marked (Done),
#              or it is labelled "ready to code" and has no plan at all
#   plan       the issue is labelled "ready to plan" and has no plan yet
#
# An issue with an open claude/issue-<N>-* pull request is skipped in both queues. That
# is what enforces "one PR at a time per issue": the next part cannot start until the
# previous one is merged (or closed).
#
# Each queue is ordered by the "priority: high|medium|low" label before it is truncated to
# CLAUDE_MAX_ITEMS, so raising an issue's priority moves it up the queue. Issues without a
# priority label sort last, behind "priority: low".
#
# Writes plan_matrix/implement_matrix (and has_plan/has_implement) to $GITHUB_OUTPUT, or
# to stdout when that is unset, so the selection can be dry-run locally with:
#
#   GH_TOKEN=$(gh auth token) bash .github/scripts/select-claude-work.sh

set -euo pipefail

readonly plan_dir="docs/plans"
readonly max_items="${CLAUDE_MAX_ITEMS:-3}"
readonly only_issue="${CLAUDE_ONLY_ISSUE:-}"

open_issues=$(gh issue list --state open --limit 200 --json number,title,labels)
open_branches=$(gh pr list --state open --limit 200 --json headRefName --jq '.[].headRefName')

# An open PR on a claude/issue-<N>-* branch means part <k> of issue <N> is still in
# review; nothing new may start for that issue.
has_open_pull_request() {
    grep -qE "^claude/issue-$1-" <<<"$open_branches"
}

plan_file_for() {
    local candidate
    for candidate in "$plan_dir"/issue-"$1"-*.md; do
        if [[ -f $candidate ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
}

# A plan is unfinished while it still has a "## PR <k>: ..." heading without the (Done)
# suffix that the implementing PR appends.
has_pending_section() {
    local total done_count
    total=$(grep -cE '^## PR [0-9]+:' "$1" || true)
    done_count=$(grep -cE '^## PR [0-9]+:.*\(Done\)[[:space:]]*$' "$1" || true)
    ((total > done_count))
}

# Sort key for a queue: lower comes first. Unlabelled issues sort behind "priority: low",
# so adding the label can only ever promote an issue, never demote it below the backlog.
priority_rank() {
    case ",$1," in
        *,"priority: high",*) echo 0 ;;
        *,"priority: medium",*) echo 1 ;;
        *,"priority: low",*) echo 2 ;;
        *) echo 3 ;;
    esac
}

# Each entry is "<rank>\t<item json>", so the arrays can be sorted before the max_items cut.
plan_items=()
implement_items=()

while IFS=$'\t' read -r number title labels; do
    if [[ -n $only_issue && $number != "$only_issue" ]]; then
        continue
    fi
    if has_open_pull_request "$number"; then
        echo "issue #$number: skipped, a claude/issue-$number-* pull request is still open" >&2
        continue
    fi

    plan_file=$(plan_file_for "$number")
    item=$(printf '%s\t%s' \
        "$(priority_rank "$labels")" \
        "$(jq -nc --argjson issue "$number" --arg title "$title" '{issue: $issue, title: $title}')")

    if [[ -n $plan_file ]]; then
        if has_pending_section "$plan_file"; then
            implement_items+=("$item")
        else
            echo "issue #$number: $plan_file has no pending section but was not deleted" >&2
        fi
    elif [[ ,$labels, == *,"ready to code",* ]]; then
        implement_items+=("$item")
    elif [[ ,$labels, == *,"ready to plan",* ]]; then
        plan_items+=("$item")
    fi
done < <(jq -r '.[] | [.number, .title, ([.labels[].name] | join(","))] | @tsv' <<<"$open_issues")

# Highest priority first, and a stable sort within a rank so that equal-priority issues
# keep the order gh listed them in.
by_priority() {
    if (($# == 0)); then
        return
    fi
    printf '%s\n' "$@" | sort -s -n -k1,1 | cut -f2-
}

mapfile -t plan_items < <(by_priority ${plan_items+"${plan_items[@]}"})
mapfile -t implement_items < <(by_priority ${implement_items+"${implement_items[@]}"})

# Finishing work already under way beats starting more of it, so implement items take
# the budget first.
selected_implement=("${implement_items[@]:0:max_items}")
remaining=$((max_items - ${#selected_implement[@]}))
selected_plan=("${plan_items[@]:0:remaining}")

as_matrix() {
    if (($# == 0)); then
        echo '{"item":[]}'
    else
        printf '%s\n' "$@" | jq -sc '{item: .}'
    fi
}

plan_matrix=$(as_matrix ${selected_plan+"${selected_plan[@]}"})
implement_matrix=$(as_matrix ${selected_implement+"${selected_implement[@]}"})

output() {
    printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT:-/dev/stdout}"
}

output plan_matrix "$plan_matrix"
output implement_matrix "$implement_matrix"
output has_plan "$([[ $plan_matrix == '{"item":[]}' ]] && echo false || echo true)"
output has_implement "$([[ $implement_matrix == '{"item":[]}' ]] && echo false || echo true)"
