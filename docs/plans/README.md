# Implementation plans

A plan in this directory is the agreed breakdown of one issue into a sequence of small
pull requests. The Claude issue automation
(`.github/workflows/claude-issues.yml`) writes them, reads them to decide what to build
next, and deletes them when the issue is finished — so the format below is load-bearing.
You are welcome to edit a plan by hand while it is in flight; keep the headings intact.

## Lifecycle

1. An issue is labelled **ready to plan**. The next run opens a pull request adding
   `issue-<N>-<slug>.md` here, and nothing else.
2. You review and merge that pull request. Merging it is what starts the work.
3. Each later run implements the first section not marked `(Done)`, appends `(Done)` to
   that heading in the same commit, and opens one pull request for it. Nothing else
   starts on that issue until you merge that pull request — but see below: while it is
   open, runs keep working *on it*.
4. The pull request for the last section deletes this file and says `Closes #<N>`, so
   merging it closes the issue.

An issue labelled **ready to code** skips step 1 and goes straight to a single pull
request — unless it turns out to need more than about 500 lines, in which case the run
writes a plan here instead.

## What a run actually does

Each step of the lifecycle above is one run, and a run is three Claude steps in one job:
it builds the change and commits, a second step reviews that commit in a fresh context
and fixes what it finds, and a third re-runs the gates and opens the pull request. So the
body you read describes the code as it stands after review, and the branch shows you what
the build did and what the review changed as separate commits.

Which step of the lifecycle to run is a decision, not a fixed sequence. A run also picks
up an open pull request of its own that has stalled — a review you left, red checks, a
conflict with `main` — and answers that instead, because that is what you are waiting on.
It may equally decide the plan itself needs revising, or that the issue is too ambiguous
to act on, in which case it asks on the issue and commits nothing.

Two things follow for you:

- **Editing a plan mid-flight works**, and so does saying what you want in a review or a
  comment on the pull request. Only comments from someone with write access to this
  repository are acted on.
- **A red run is worth looking at.** A run that fails for its own reasons labels the issue
  `claude: stuck` and the scheduled runs leave it alone until you remove that label (or
  rerun that issue alone from the Actions tab). A run that simply ran out of subscription
  usage ends green and is picked up by the next scheduled run, untouched.

## Format

```markdown
# Issue #123: Rebuild the crawler's retry backoff

One paragraph: the problem, and what the code looks like when this is finished.

## PR 1: Record retry attempts on CrawlAttempt
**Estimated added lines:** ~180
**Files:** mwmbl/crawler/models.py, mwmbl/crawler/retry.py, test/test_retry.py

What to change and which existing code to reuse.

- Acceptance criterion
- Acceptance criterion

## PR 2: Back off exponentially on 5xx
...
```

What the automation parses, and what a hand edit must therefore preserve:

- A section heading matches `## PR <k>: <title>` exactly, numbered from 1, in the order
  they are to be built.
- A finished section's heading ends with ` (Done)` and nothing after it.
- The file is named `issue-<N>-<slug>.md`, where `<N>` is the issue number.
- The file is deleted by the last pull request. A plan whose sections are all `(Done)`
  but which still exists is reported as a warning by the selector and blocks nothing.

Each section must be at most **500 added lines** including tests, must be independently
mergeable, and must leave `main` working.
