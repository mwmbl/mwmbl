#!/usr/bin/env bash
#
# Say on the pull request that this run changed nothing, and why it was woken.
#
# The comment is load-bearing rather than courtesy. The selector's queues are pure
# functions of repository state, so a run that ends without committing leaves exactly the
# state that selected it: the same red checks, the same unanswered review. Without a record
# that the automation has already had its turn at this commit, the item would be selected
# again on the next trigger and on every scheduled run after that, for as long as the
# reason stood.
#
# So this is what bounds the retries: select-claude-work.sh stops counting failing checks
# and conflicts once the automation has spoken about the current commit, and counts
# feedback only from after that. A new commit, or a human replying, is what starts the pull
# request moving again.
#
# A run that answered a question or asked one has already spoken, with a comment carrying
# the same marker, so this stays quiet rather than saying it twice. The marker prefix has
# to stay in step with the one select-claude-work.sh looks for.

set -euo pipefail

pull_request="${PULL_REQUEST:?}"
reason="${REASON:?}"
readonly marker='<!-- claude-run:'

# Piped through jq rather than gh's own --jq, which takes the expression alone and
# rejects the --arg the marker needs.
already_spoken=$(gh pr view "$pull_request" --json commits,comments \
    | jq -r --arg marker "$marker" '
        (.commits | last | .committedDate) as $commit
        | [.comments[] | select(.body | contains($marker)) | .createdAt]
        | max
        | . != null and . > $commit')

if [[ $already_spoken == true ]]; then
    echo "::notice::The run already commented on #$pull_request; not saying it twice."
    exit 0
fi

gh pr comment "$pull_request" --body "This run looked at the pull request and left the
branch as it found it.

It was woken because: $reason.

It will not try again by itself at this commit — pushing a commit here, or replying, is
what wakes it.

$marker no change -->"
