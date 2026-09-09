#!/usr/bin/env bash
#
# Pick the issues the Claude automation should act on, and in what order.
#
# This is a cost gate, not an instruction. It answers one question — is it worth spending
# a run on this issue at all — because deciding that needs to be cheap, and everything a
# run does after that point costs Opus turns. What it works out about *what* an issue
# needs is a hint: the run re-derives it from the thread, which the selector has never
# read, and .github/claude/run.md tells it to overrule this when the two disagree.
#
# So the kinds below order the queues and name the job; they do not direct the work.
#
#   respond    an open claude/issue-<N>-* pull request has something outstanding
#   implement  a plan under docs/plans/ has a section not marked (Done), or the issue is
#              labelled "ready to code" and has no plan at all
#   plan       the issue is labelled "ready to plan" and has no plan yet
#
# Finishing beats starting, so respond items come first. The queues are derived entirely
# from repository and issue state, never from a label the automation has to remember to
# set, so a rerun after a failed job is harmless and a human can redirect the work by
# editing a plan file or leaving a review.
#
# One pull request at a time per issue: an issue with an open claude/issue-<N>-* pull
# request can only produce a "respond" item, never a new branch.
#
# Pull requests from forks are not the automation's to work on and are ignored throughout:
# a fork's branch is code nobody here has reviewed, and the workflow could not check it
# out with a working token even if it wanted to.
#
# The one label the automation writes is "claude: stuck", added by the workflow when a run
# fails for a reason that is not a usage limit. Items on a stuck issue are skipped so a
# broken item cannot burn the budget on every scheduled run; a human removes the label, or
# reruns that issue alone with workflow_dispatch, which ignores it.
#
# Each kind is ordered by the "priority: high|medium|low" label before the queue is
# truncated to CLAUDE_MAX_ITEMS. Issues without a priority label sort last.
#
# Writes items_matrix (and has_items) to $GITHUB_OUTPUT, or to stdout when that is unset,
# so the selection can be dry-run locally with:
#
#   GH_TOKEN=$(gh auth token) bash .github/scripts/select-claude-work.sh

set -euo pipefail

readonly plan_dir="docs/plans"
readonly max_items="${CLAUDE_MAX_ITEMS:-3}"
readonly only_issue="${CLAUDE_ONLY_ISSUE:-}"

# Every comment the automation posts on a pull request ends with one of these, so
# recognising its own voice is a string test on text it wrote rather than a guess about
# authorship. That matters: gh's projection of a comment author carries a login and nothing
# else — no bot flag, and a Bot actor's login arrives without the "[bot]" suffix — so the
# obvious authorship test silently matches nothing and the automation reads its own
# comments as a maintainer's.
#
# The prefix says the comment is the automation's, and never counts as feedback. The
# narrower marker says something stronger: this run had its turn at the current commit and
# changed nothing. Only a run that commits nothing writes that one, because a run that
# pushes has not yet had a turn at what it just pushed — CI has not run on it. Kept in step
# with section 5 of .github/claude/run.md, which writes both.
readonly marker='<!-- claude-run:'
readonly turn_taken='<!-- claude-run: done -->'

open_issues=$(gh issue list --state open --limit 200 --json number,title,labels)
open_pull_requests=$(gh pr list --state open --limit 200 \
    --json number,headRefName,isCrossRepository \
    --jq '[.[] | select(.isCrossRepository | not)]')

# Why one open pull request is worth waking a run for, or nothing if it is not.
#
# Feedback counts only from an OWNER, MEMBER or COLLABORATOR — a drive-by comment on a
# public repository must not be able to steer a run — and only when it is newer than the
# last commit on the branch, which is what "already addressed" means here. A review
# carrying inline comments and no body still arrives as a review with a timestamp, so
# inline-only feedback is caught too.
#
# A run that ends without committing changes nothing the queues are derived from, so
# failing checks it could not fix, or a question it could not answer without guessing,
# would select this pull request again on every trigger for as long as they stood. Saying
# so is what bounds the retries: once a run has had its turn at a commit, the mechanical
# reasons stop counting for that commit, and only something said afterwards counts as
# feedback. A new commit, or a human replying, is what starts it moving again.
readonly wake_filter='
def ours: (.body // "") | contains($marker);
def had_a_turn: (.body // "") | contains($turn_taken);
def maintainer: .authorAssociation | IN("OWNER", "MEMBER", "COLLABORATOR");
def failing: [.statusCheckRollup[] | select(.conclusion == "FAILURE") | .name];

(.commits | last | .committedDate) as $commit
| ([.comments[] | select(had_a_turn) | .createdAt] | max) as $spoken
| ([ (.comments[] | select(ours | not) | select(maintainer) | .createdAt),
     (.reviews[] | select(maintainer) | .submittedAt) ] | max) as $feedback
| if $feedback != null and $feedback > $commit
       and ($spoken == null or $feedback > $spoken)
  then "feedback nobody has answered"
  elif $spoken != null and $spoken > $commit then empty
  elif (failing | length) > 0 then "failing checks: " + (failing | join(", "))
  elif .mergeable == "CONFLICTING" then "conflicts with main"
  else empty end
'

reason_to_wake() {
    gh pr view "$1" --json mergeable,statusCheckRollup,commits,comments,reviews \
        | jq -r --arg marker "$marker" --arg turn_taken "$turn_taken" "$wake_filter"
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
# suffix that the implementing pull request appends.
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

    emit() {
        printf '%s\t%s' "$rank" "$(jq -nc \
            --argjson issue "$number" --arg title "$title" --arg kind "$1" \
            --argjson pr "${2:-0}" --arg reason "${3:-}" \
            '{issue: $issue, title: $title, kind: $kind, pr: $pr, reason: $reason}')"
    }

    pull_request=$(pull_request_for "$number")
    plan_file=$(plan_file_for "$number")

    if [[ -n $pull_request ]]; then
        reason=$(reason_to_wake "$pull_request")
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
