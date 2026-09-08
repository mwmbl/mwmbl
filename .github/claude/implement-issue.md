# Implement the next part of an issue

You are Claude Code running in GitHub Actions on a fresh checkout of `main`, with the
project installed and a test database running. Your job is **one** commit: the next
unbuilt part of one issue, verified green.

`ISSUE_NUMBER` is in the prompt; `$N` below means that number. Later steps in this job
review what you commit and open the pull request, so commit and stop — see
`.github/claude/coordinate.md` for the pipeline.

## 1. Decide what to build

Read `AGENTS.md` and `gh issue view $N --comments` first.

- **If `docs/plans/issue-$N-*.md` exists**, take the **first** `## PR k:` heading that is
  not marked `(Done)`. Build exactly that section — not the next one, not a bit of the
  next one. Note whether it is the last unfinished section in the file.
- **Otherwise** implement the whole issue in this one commit. If it cannot be done
  in about 500 added lines, do not start: write a plan file instead, following sections 2
  and 3 of `.github/claude/plan-issue.md`, and stop there. The next run implements its
  first section.

## 2. Build it

Branch off `main`: `git checkout -b claude/issue-$N-pr-<k>`, where `<k>` is the section
number, or `1` when there is no plan.

Write the code the way the surrounding code is written, and follow `AGENTS.md`. Add tests
under `test/` alongside the behaviour they cover.

## 3. Both gates must pass before you commit

```
make check
uv run pytest
```

`DATABASE_URL` and `DJANGO_SETTINGS_MODULE` are already set for you. Iterate until both
are green. If something is genuinely beyond this change to fix, say exactly what fails
and why in your final message — never describe a run as green when it is not.

## 4. Check the size budget

```
git diff --numstat origin/main...HEAD
```

Sum the added lines, ignoring `uv.lock`, `poetry.lock`, `devdata/`, `front-end/` build
output and `docs/plans/`. If the total is over 500, cut the change back to the part that
stands on its own, and — when there is a plan — add a follow-up section to it for the
remainder rather than silently dropping the work.

## 5. Update the plan in the same commit

- Not the last unfinished section: append ` (Done)` to that section's heading, so it
  reads `## PR k: <title> (Done)`. Change nothing else in the plan.
- The last unfinished section: `git rm` the whole plan file. The issue is finished.
- No plan at all: nothing to update.

## 6. Commit it

```
git commit -m "<section title>"
```

Stop there — do not push, do not open a pull request. Leave the finalise step what it
needs for the body in your final message: what changed and why, how you verified it (the
actual `make check` and `uv run pytest` results), and whether this was the last section of
the plan, which decides between `Part of #$N` and `Closes #$N`.

## Rules

- You may change `.github/` when the issue calls for it. Editing an instruction file
  you are following, or the workflow you are running under, takes effect only once a
  maintainer merges the pull request — never part-way through this run.
- One section per run. Never bundle two sections into one pull request.
- Never `git push`, never `gh pr create`, do not merge anything, do not change labels.
- If you cannot finish, commit nothing and explain in your final message. The review and
  finalise steps are skipped when there is no new commit, and a half-finished branch that
  reaches a pull request blocks every later part of the issue.
