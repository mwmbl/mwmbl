#!/usr/bin/env bash
#
# Pick the work items the Claude issue-automation workflow should act on.
#
# An item is one issue plus the kind of attention it needs. The kinds are ordered so
# that finishing something always beats starting something:
#
#   respond    an open claude/issue-<N>-* pull request needs a change: someone with
#              write access left feedback after the last commit, its checks are red, or
#              it no longer merges cleanly
#   implement  the issue has a plan under docs/plans/ with a section not marked (Done),
#              or it is labelled "ready to code" and has no plan at all
#   plan       the issue is labelled "ready to plan" and has no plan yet
#
# The queues are derived entirely from repository and issue state, never from a label the
# automation has to remember to set, so a rerun after a failed job is harmless and a human
# can redirect the work by editing a plan file or leaving a review.
#
# Still one pull request at a time per issue: an issue with an open claude/issue-<N>-*
# pull request can only produce a "respond" item, never a new branch. What changed is that
# such an issue is no longer simply skipped — an unanswered review is the most valuable
# thing the automation can pick up, because it is what a human is waiting on.
#
# Pull requests from forks are not the automation's to work on and are ignored throughout,
# branch name notwithstanding: a fork's branch is code nobody here has reviewed, and the
# workflow could not check it out with a working token even if it wanted to.
#
# The one label the automation writes is "claude: stuck", added by the workflow when a run
# fails for a reason that is not a usage limit. Items on a stuck issue are skipped so a
# broken item cannot burn the budget on every scheduled run; a human removes the label, or
# reruns that issue alone with workflow_dispatch, which ignores it.
#
# Each kind is ordered by the "priority: high|medium|low" label before the queue is
# truncated to CLAUDE_MAX_ITEMS, so raising an issue's priority moves it up. Issues without
# a priority label sort last, behind "priority: low".
#
# Writes items_matrix (and has_items) to $GITHUB_OUTPUT, or to stdout when that is unset,
# so the selection can be dry-run locally with:
#
#   GH_TOKEN=$(gh auth token) bash .github/scripts/select-claude-work.sh

set -euo pipefail

readonly plan_dir="docs/plans"
readonly max_items="${CLAUDE_MAX_ITEMS:-3}"
readonly only_issue="${CLAUDE_ONLY_ISSUE:-}"

open_issues=$(gh issue list --state open --limit 200 --json number,title,labels)
open_pull_requests=$(gh pr list --state open --limit 200 \
    --json number,headRefName,isCrossRepository \
    --jq '[.[] | select(.isCrossRepository | not)]')

# What a run that changed nothing leaves behind, so that it is not asked the same question
# on the next trigger and every scheduled run after it. Written by
# .github/scripts/report-no-change.sh — the two strings have to stay in step.
readonly no_change_marker='<!-- claude-run: no change -->'

# What needs answering on one open pull request, if anything.
#
# Feedback counts only from an OWNER, MEMBER or COLLABORATOR — a drive-by comment on a
# public repository must not be able to steer a run — and only when it is newer than the
# last commit on the branch, which is what "already addressed" means here. A review
# carrying inline comments and no body still arrives as a review with a timestamp, so
# inline-only feedback is caught too.
#
# The last thing a run said about the current commit bounds the retries. A run that ends
# without committing changes nothing the queues are derived from, so failing checks it
# could not fix, or a question it could not answer without guessing, would select this
# pull request again on every trigger for as long as they stood. Once it has reported on a
# commit, the mechanical reasons stop counting for that commit and only something said
# after the report counts as feedback — a new commit, or a human replying, is what starts
# it again.
#
# Comments and reviews come from the REST API rather than from `gh pr view`, which is the
# only one of the two that says whether an author is a bot: GraphQL gives a Bot actor a
# login without the "[bot]" suffix, so a suffix test against gh's projection silently
# matches nothing and the automation reads its own comments as a maintainer's.
#
# Asked per pull request rather than across the list: `gh pr list --json commits` walks
# every author of every commit of every open pull request, which exceeds GitHub's GraphQL
# node limit on a repository this size.
readonly reason_filter='
def is_maintainer:
    (.author_association | . == "OWNER" or . == "MEMBER" or . == "COLLABORATOR")
    and (.user.type != "Bot");

($state.commits | last | .committedDate) as $last_commit
| ([ $comments[]
     | select(.user.type == "Bot")
     | select(.body | contains($marker))
     | .created_at ] | max) as $reported
| ([ ($reviews[] | select(is_maintainer) | .submitted_at),
     ($comments[] | select(is_maintainer) | .created_at) ] | max) as $feedback
| ($reported != null and $reported > $last_commit) as $already_reported
| [ (if $feedback != null and $feedback > $last_commit
        and ($reported == null or $feedback > $reported)
     then "unanswered feedback" else empty end),
    (if $already_reported then empty
     else ([ $state.statusCheckRollup[] | select(.conclusion == "FAILURE") | .name ]
           | if length > 0 then "failing checks: " + join(", ") else empty end)
     end),
    (if $state.mergeable == "CONFLICTING" and ($already_reported | not)
     then "conflicts with main" else empty end) ]
| join("; ")
'

# `gh api --paginate` prints one JSON document per page, which jq -s puts back together.
whole_of() {
    gh api "$1" --paginate | jq -s 'add // []'
}

reason_to_respond() {
    local state comments reviews
    state=$(gh pr view "$1" --json mergeable,statusCheckRollup,commits)
    comments=$(whole_of "repos/{owner}/{repo}/issues/$1/comments")
    reviews=$(whole_of "repos/{owner}/{repo}/pulls/$1/reviews")
    jq -nr --argjson state "$state" --argjson comments "$comments" \
        --argjson reviews "$reviews" --arg marker "$no_change_marker" "$reason_filter"
}

pull_request_for() {
    jq -r --arg prefix "claude/issue-$1-" \
        'map(select(.headRefName | startswith($prefix))) | first | .number // empty' \
        <<<"$open_pull_requests"
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

# Each entry is "<rank>\t<item json>", so a queue can be sorted before the max_items cut.
respond_items=()
implement_items=()
plan_items=()

while IFS=$'\t' read -r number title labels; do
    if [[ -n $only_issue && $number != "$only_issue" ]]; then
        continue
    fi
    # workflow_dispatch on one issue is the escape hatch from a stuck label, so the skip
    # only applies to the untargeted queues.
    if [[ -z $only_issue && ,$labels, == *,"claude: stuck",* ]]; then
        echo "issue #$number: skipped, labelled 'claude: stuck'" >&2
        continue
    fi

    rank=$(priority_rank "$labels")
    plan_file=$(plan_file_for "$number")

    emit() {
        printf '%s\t%s' "$rank" "$(jq -nc \
            --argjson issue "$number" --arg title "$title" --arg kind "$1" \
            --argjson pr "${2:-0}" --arg reason "${3:-}" \
            '{issue: $issue, title: $title, kind: $kind, pr: $pr, reason: $reason}')"
    }

    pull_request=$(pull_request_for "$number")

    if [[ -n $pull_request ]]; then
        reason=$(reason_to_respond "$pull_request")
        if [[ -n $reason ]]; then
            respond_items+=("$(emit respond "$pull_request" "$reason")")
        else
            echo "issue #$number: skipped, claude/issue-$number-* is open and up to date" >&2
        fi
    elif [[ -n $plan_file ]]; then
        if has_pending_section "$plan_file"; then
            implement_items+=("$(emit implement)")
        else
            echo "issue #$number: $plan_file has no pending section but was not deleted" >&2
        fi
    elif [[ ,$labels, == *,"ready to code",* ]]; then
        implement_items+=("$(emit implement)")
    elif [[ ,$labels, == *,"ready to plan",* ]]; then
        plan_items+=("$(emit plan)")
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

mapfile -t ordered < <(
    by_priority ${respond_items+"${respond_items[@]}"}
    by_priority ${implement_items+"${implement_items[@]}"}
    by_priority ${plan_items+"${plan_items[@]}"}
)
selected=("${ordered[@]:0:max_items}")

if ((${#selected[@]} == 0)); then
    items_matrix='{"item":[]}'
else
    items_matrix=$(printf '%s\n' "${selected[@]}" | jq -sc '{item: .}')
fi

output() {
    printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT:-/dev/stdout}"
}

output items_matrix "$items_matrix"
output has_items "$([[ $items_matrix == '{"item":[]}' ]] && echo false || echo true)"
