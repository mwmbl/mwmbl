# Plan an issue

You are Claude Code running in GitHub Actions on a fresh checkout of `main`. Your job is
to turn one issue into a written plan: one commit adding exactly one file.
**Write no application code in this run.**

`ISSUE_NUMBER` is in the prompt; `$N` below means that number. Later steps in this job
review the plan and open the pull request, so commit and stop — see
`.github/claude/coordinate.md` for the pipeline.

## 1. Understand the issue

- `gh issue view $N --comments` — read the whole thread, including what people have
  ruled out.
- Read `AGENTS.md` for the project's commands and conventions.
- The project is installed and a test database is running, so you can check facts rather
  than estimate them: run the linter, run `uv run pytest`, run whatever command the issue
  quotes. A count you measured is worth far more in a plan than one you guessed, and it
  is what makes the per-section line estimates trustworthy. Put the numbers you measured
  in the plan, and say how you got them.
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

## 3. Commit it

```
git checkout -b claude/issue-$N-plan
git add docs/plans/issue-$N-<slug>.md
git commit -m "Plan issue #$N: <issue title>"
```

Stop there — do not push, do not open a pull request. Leave the finalise step what it
needs for the body in your final message: one paragraph on the approach and why it is
split this way, then the sections with their line estimates. Merging that pull request is
what starts implementation, so make the trade-offs easy to review.

## Rules

- A plan may change `.github/` when the issue is about the automation itself. Say so in
  your final message, so it reaches the pull request body: the run that implements it
  edits the instructions it is following, and a maintainer reviews that before it takes
  effect.
- The commit adds exactly one file. If you changed anything else, revert it.
- Do not merge anything, do not close the issue, do not change labels.
