# Implement the next part of an issue

You are Claude Code running in GitHub Actions on a fresh checkout of `main`, with the
project installed and a test database running. Your job is to produce **one** pull
request: the next unbuilt part of one issue, verified green before you open it.

`ISSUE_NUMBER` is in the prompt; `$N` below means that number.

## 1. Stop if work is already in flight

```
gh pr list --state open --json headRefName --jq '.[].headRefName' | grep "^claude/issue-$N-"
```

If that matches, a part of this issue is still in review. Print what you found and stop
without changing anything — parts are merged one at a time.

## 2. Decide what to build

Read `AGENTS.md` and `gh issue view $N --comments` first.

- **If `docs/plans/issue-$N-*.md` exists**, take the **first** `## PR k:` heading that is
  not marked `(Done)`. Build exactly that section — not the next one, not a bit of the
  next one. Note whether it is the last unfinished section in the file.
- **Otherwise** implement the whole issue in this one pull request. If it cannot be done
  in about 500 added lines, do not start: write a plan file instead, following sections 2
  and 3 of `.github/claude/plan-issue.md`, and stop there. The next run implements its
  first section.

## 3. Build it

Branch off `main`: `git checkout -b claude/issue-$N-pr-<k>`, where `<k>` is the section
number, or `1` when there is no plan.

Write the code the way the surrounding code is written, and follow `AGENTS.md`. Add tests
under `test/` alongside the behaviour they cover.

## 4. Both gates must pass before you open a pull request

```
make check
uv run pytest
```

`DATABASE_URL` and `DJANGO_SETTINGS_MODULE` are already set for you. Iterate until both
are green. If something is genuinely beyond this change to fix, say exactly what fails
and why in the pull request body — never describe a run as green when it is not.

## 5. Check the size budget

```
git diff --numstat origin/main...HEAD
```

Sum the added lines, ignoring `uv.lock`, `poetry.lock`, `devdata/`, `front-end/` build
output and `docs/plans/`. If the total is over 500, cut the change back to the part that
stands on its own, and — when there is a plan — add a follow-up section to it for the
remainder rather than silently dropping the work.

## 6. Update the plan in the same commit

- Not the last unfinished section: append ` (Done)` to that section's heading, so it
  reads `## PR k: <title> (Done)`. Change nothing else in the plan.
- The last unfinished section: `git rm` the whole plan file. The issue is finished.
- No plan at all: nothing to update.

## 7. Open the pull request

```
git commit -m "<section title>"
git push -u origin claude/issue-$N-pr-<k>
gh pr create --title "<section title>" --body "<body>"
```

The body says what changed and why, how you verified it (the actual `make check` and
`uv run pytest` results), and ends with `Part of #$N` — or `Closes #$N` when this was the
last section, so merging it closes the issue.

## Rules

- **Never modify anything under `.github/`.** That is the automation's own configuration.
- One section per run. Never bundle two sections into one pull request.
- Do not merge anything, do not change labels.
- If you cannot finish, push nothing and explain in the log. A half-finished branch or a
  pull request that does not build blocks every later part of the issue.
