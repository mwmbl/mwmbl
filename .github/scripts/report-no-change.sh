#!/usr/bin/env bash
#
# Say on the pull request that this run changed nothing, and why it was woken.
#
# The comment is load-bearing rather than courtesy. The selector's queues are pure
# functions of repository state, so a run that ends without committing leaves exactly the
# state that selected it: the same red checks, the same unanswered review. Without a record
# that the automation has already had its turn at this commit, the item would be selected
# again on the next trigger and on every scheduled run after that, at three Opus steps a
# time, for as long as the reason stood.
#
# So this is what bounds the retries: select-claude-work.sh stops counting failing checks
# and conflicts once a report is newer than the last commit, and counts feedback only from
# after it. A new commit, or a human replying, is what starts the pull request moving
# again.
#
# The marker string has to stay in step with the one select-claude-work.sh looks for.

set -euo pipefail

pull_request="${PULL_REQUEST:?}"
reason="${REASON:?}"
readonly marker='<!-- claude-run: no change -->'

gh pr comment "$pull_request" --body "This run looked at the pull request and left the
branch as it found it.

It was woken because: $reason.

It will not try again by itself at this commit — pushing a commit here, or replying, is
what wakes it.

$marker"
