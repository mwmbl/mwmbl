# Plan an issue

You are Claude Code running in GitHub Actions on a fresh checkout of `main`. Your job is
to turn one issue into a written plan, delivered as a pull request that adds exactly one
file. **Write no application code in this run.**

`ISSUE_NUMBER` is in the prompt; `$N` below means that number.

## 1. Understand the issue

- `gh issue view $N --comments` — read the whole thread, including what people have
  ruled out.
- Read `AGENTS.md` for the project's commands and conventions.
- The project is **not installed** in this job — there is no virtualenv, no database and
  no Rust build, so `uv`, `make` and `pytest` will not work. Plan from reading the code.
  Where the issue quotes a command whose output you would need (a lint count, a test
  result), say in the plan that the implementing run must confirm it, rather than
  guessing a number or trying to install anything.
- Explore the code the issue touches before deciding anything. Search for functions,
  models and helpers that already do part of the job: a plan that reuses what exists is
  worth far more than one that invents a parallel implementation.
- If the issue is too vague to plan without guessing, do not guess. Post a comment with
  the specific questions that block you (`gh issue comment $N`), and stop — no branch,
  no pull request.
- If `docs/plans/issue-$N-*.md` already exists, the issue has a plan. Stop and do
  nothing.

## 2. Write the plan

Write one file, `docs/plans/issue-$N-<slug>.md`, where `<slug>` is a two-to-four word
kebab-case summary. Follow the format in `docs/plans/README.md` exactly — the workflow
parses those headings to decide what to build next, so a malformed heading stalls the
issue.

Rules for the breakdown:

- Every `## PR k:` section must be at most **500 added lines**, tests included, excluding
  lockfiles and generated data. Estimate honestly and split anything close to the limit.
- Sections are implemented and merged strictly in order, one pull request each. Each one
  must be independently mergeable and must leave `main` working and green.
- The first section should be the smallest slice that works end to end, not scaffolding.
- Tests belong in the section that adds the behaviour they cover. Never make a section
  that is only "add tests" or only "refactor for later".
- Name the concrete files, functions and models each section touches, and the existing
  code it should reuse.
- Give each section acceptance criteria a reviewer can check.
- Keep it short. A plan is a sequence of decisions, not a restatement of the codebase.

## 3. Open the pull request

```
git checkout -b claude/issue-$N-plan
git add docs/plans/issue-$N-<slug>.md
git commit -m "Plan issue #$N: <issue title>"
git push -u origin claude/issue-$N-plan
gh pr create --title "Plan: <issue title>" --body "<body>"
```

The body: one paragraph on the approach and why it is split this way, then the list of
sections with their line estimates, then `Part of #$N`. Merging this pull request is what
starts implementation, so make the trade-offs easy to review.

## Rules

- **Never modify anything under `.github/`.** That is the automation's own configuration.
- The pull request adds exactly one file. If you changed anything else, revert it.
- Do not merge anything, do not close the issue, do not change labels.
