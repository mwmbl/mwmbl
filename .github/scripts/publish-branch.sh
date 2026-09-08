#!/usr/bin/env bash
#
# Push the branch the run left behind, and open or update its pull request.
#
# This is the only thing that pushes, and it is a shell script rather than a Claude step
# for two reasons.
#
# The first is that it is mechanical: verify, push, create or comment. The run that did
# the work already knows everything a pull request body needs, so there is nothing left
# here for a model to work out — it writes the body to a file in $REPORT_DIR and this
# posts it.
#
# The second is the point. Everything after the run starts is the run's own account of
# itself, and the two things that must be true before a change reaches a human are exactly
# the two a self-report cannot establish: that the gates pass, and that somebody other than
# the author looked at it. So this measures the gates itself rather than believing the
# summary, and refuses to push at all unless the run left a review report — which only
# .github/claude/review-changes.md, running as its own subagent, produces. A run that skips
# the review does not get a shortcut; it gets nothing published.
#
# Nothing to publish is the ordinary outcome, not a failure: a run that answered a
# question, asked one, or found the work already done leaves the branch exactly as it
# found it, and this exits green having said so.

set -euo pipefail

readonly report_dir="${CLAUDE_REPORT_DIR:?}"
readonly review_report="$report_dir/review.md"
readonly body_file="$report_dir/pr-body.md"

# Whether a pull request now carries this run's work. The workflow reads it to decide
# whether to leave the "this run changed nothing" comment that bounds the retries.
published() {
    printf 'published=%s\n' "$1" >>"${GITHUB_OUTPUT:-/dev/stdout}"
}

branch=$(git branch --show-current)

if [[ $branch != claude/* ]]; then
    echo "::notice::Not on a claude/* branch; nothing to publish."
    published false
    exit 0
fi

# Measured against the branch's own published tip rather than against the commit the job
# checked out. A respond run starts with `gh pr checkout`, which moves HEAD to commits that
# are already pushed and already reviewed: comparing against where the job started would
# call every one of those runs a build.
published_tip="origin/$branch"
if ! git rev-parse --verify --quiet "$published_tip" >/dev/null; then
    published_tip=origin/main
fi
if [[ $(git rev-parse HEAD) == $(git rev-parse "$published_tip") ]]; then
    echo "::notice::$branch is unchanged from $published_tip; nothing to publish."
    published false
    exit 0
fi

# The attestation. Not a formality: the review is a separate context precisely so that its
# verdict is not the author's, and a report that does not exist is a review that did not
# happen. Refusing here is what stops "I reviewed it" from being a claim the run can simply
# make about itself.
if [[ ! -s $review_report ]]; then
    echo "::error::$branch has commits but no review report at $review_report." \
        "Section 4 of .github/claude/run.md is not optional: a change is published only" \
        "once a review subagent has read it in a fresh context. Pushing nothing."
    exit 1
fi
if [[ ! -s $body_file ]]; then
    echo "::error::$branch has commits but no pull request body at $body_file." \
        "Section 4 of .github/claude/run.md writes it. Pushing nothing."
    exit 1
fi

# The Claude GitHub App's token, as the Claude step's github_token output. Not the workflow
# token: a push made with GITHUB_TOKEN does not trigger workflows, so the pull request this
# opens would sit with no checks on it, forever, and the automation would never see CI go
# red on its own branch. Unset means the app authentication the whole workflow assumes did
# not happen, which is worth failing on rather than working around — but only once there is
# something to push, so that a run with nothing to publish stays green regardless.
readonly token="${GH_TOKEN:?}"
# git prints the remote it failed to reach, and from here that remote carries the token.
echo "::add-mask::$token"

# Nothing on this branch has been checked since the last commit landed, and the run's own
# account of the gates is the run's, not ours.
echo "::group::make check"
make check
echo "::endgroup::"
echo "::group::uv run pytest"
uv run pytest
echo "::endgroup::"

# `grep -v` exits 1 when it filters everything out, which under pipefail would end the
# script — and a plan branch, whose only file is under docs/plans/, hits that every time.
added=$(git diff --numstat "origin/main...HEAD" \
    | { grep -vE $'\t(uv\\.lock|poetry\\.lock|devdata/|front-end/|docs/plans/)' || true; } \
    | awk '{ total += $1 } END { print total + 0 }')

verified=$(cat <<EOF
---
\`make check\` and \`uv run pytest\` both pass, measured by the publish step after the
review, on $(git rev-parse --short HEAD). Added lines outside lockfiles, \`devdata/\`,
\`front-end/\` and \`docs/plans/\`: $added.
EOF
)

# Reported rather than acted on: the change has already been reviewed, and cutting it back
# now would mean republishing work nobody has looked at.
if ((added > 500)); then
    verified+=$'\n\n**This is over the 500-line budget.** Reported rather than trimmed here,'
    verified+=$' because trimming it now would republish unreviewed work.'
fi

body=$(cat "$body_file")$'\n\n'"$verified"

# actions/checkout leaves behind an http.extraheader carrying the workflow token, and it
# takes precedence over every other credential git could find, so the app token has to
# replace it rather than sit alongside it.
# --unset-all exits 5 when the key is not there, which is the state it is trying to reach.
git config --local --unset-all 'http.https://github.com/.extraheader' || true
git remote set-url origin \
    "https://x-access-token:${token}@github.com/${GITHUB_REPOSITORY:?}.git"

# A rebase cannot fast-forward. The run says so on the first line of the review report,
# which is the only place it is allowed to speak to this step.
if head -1 "$review_report" | grep -qx 'rebased: true'; then
    git push --force-with-lease -u origin "$branch"
else
    git push -u origin "$branch"
fi

existing=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number // empty')

if [[ -z $existing ]]; then
    # The commit subject already names the work: an implementing run commits the plan
    # section's title, a planning run commits "Plan issue #<N>: <title>".
    gh pr create --title "$(git log -1 --format=%s)" --body "$body"
else
    # A reviewer coming back wants to see what moved since they left, not a description
    # that quietly changed under them, so the original body stands and the update is a
    # comment.
    gh pr comment "$existing" --body "$body

<!-- claude-run: update -->"
fi

published true
