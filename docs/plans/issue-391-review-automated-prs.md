# Issue #391: Review code in automated PRs

Today the `implement` job is a single Claude invocation that writes the code, runs the
gates, pushes and opens the pull request — the author is the only reader the change ever
gets before a human sees it. When this is finished that job is a pipeline: build, then
one or more review passes in a fresh context that fix what they find and commit, then a
finalise step that re-runs the gates and opens the pull request. Each pass is a separate
`anthropics/claude-code-action@v1` step, which is what makes the context clean and lets
the passes use different models; the steps are chained by repository state (commits on
the branch), not by anything a step has to remember to set.

**Precondition — a human has to apply these, or lift a rule first.** Every section below
edits `.github/`, and `.github/claude/implement-issue.md` tells implementing runs "Never
modify anything under `.github/`". The automation therefore cannot build this issue, and
it cannot grant itself the exception either, since doing so means editing `.github/`.
Either a maintainer applies these two changes by hand (as every `.github/` change in the
history has been), or a maintainer first carves out an exception for issues whose subject
*is* the automation. This plan is written so that either route follows the same steps.

Measured on `main` at 5ccc6e2, in the same environment the automation runs in:
`make check` is green; `uv run pytest` is `730 passed, 10 skipped, 4 deselected` in 67s.
That 67s is the cost of one extra gate run per pass, and is why the job timeout has to
grow. `claude-issues.yml` is 204 lines, `implement-issue.md` 85. `pyyaml==6.0` is already
a direct dependency (`pyproject.toml:19`), so the wiring test below adds no package;
`actionlint` is not available on the runner, so the workflow is validated by parsing it
in pytest rather than by a linter.

## PR 1: Build, review once, then finalise
**Estimated added lines:** ~305
**Files:** `.github/workflows/claude-issues.yml`, `.github/claude/implement-issue.md`,
`.github/claude/review-changes.md` (new), `.github/claude/finalise-pull-request.md`
(new), `test/test_claude_automation_workflow.py` (new), `docs/plans/README.md`

The rewiring has to land in one change: a build step that no longer pushes is broken
until the finalise step exists, and every merge to `main` starts a fresh run of this
workflow, so a half-wired pipeline breaks every automated pull request in flight.

- `implement-issue.md`: keep sections 1–6 as they are. Replace section 7 so the run
  **commits on `claude/issue-$N-pr-<k>` and stops** — no `git push`, no `gh pr create`,
  no PR body. The plan-file update in section 6 stays in that commit. Say explicitly
  that later steps in the same job review and push the work.
- `review-changes.md` (new): the reviewer's charter. It gets `ISSUE_NUMBER` and
  `REVIEW_PASS` in the prompt and starts with no memory of the build, so it reads
  `AGENTS.md`, `gh issue view $N`, the plan section it is reviewing, and
  `git diff origin/main...HEAD`. It fixes what it finds, re-runs `make check` and
  `uv run pytest`, and commits `Address review pass <n>`. Rules: never push, never open a
  pull request, never amend or rebase the build commit, never weaken or delete a test to
  make a gate pass, never touch the plan file, and stay inside the section's scope —
  anything larger is a note for the pull request body, not a commit. Finding nothing is a
  valid outcome: commit nothing and say so.
- `finalise-pull-request.md` (new): re-run both gates, check the size budget with
  `git diff --numstat origin/main...HEAD` under the existing exclusions, confirm the plan
  update from section 6 is present, then push and `gh pr create`. The body says what
  changed, what each review pass changed, and quotes the gate results *it* just measured
  — a real improvement on today, where the body describes the code as it was before any
  review.
- `claude-issues.yml`: split the single `Implement the next part` step into
  `Implement the next part` (opus-5, `--max-turns 100`), a plain shell step
  `id: built` that sets `built=true` when `git rev-list --count origin/main..HEAD` is
  non-zero and `HEAD` is on a `claude/issue-<N>-` branch, then `Review pass 1` (opus-5,
  `--max-turns 40`, `REVIEW_PASS: 1`) and `Finalise the pull request` (opus-5,
  `--max-turns 30`), both guarded by `if: steps.built.outputs.built == 'true'`. The gate
  is derived from git state rather than an action output, so a rerun is as harmless as
  the selector's queues are. Raise `timeout-minutes` from 60 to 120 and extend the job's
  header comment.
- `test/test_claude_automation_workflow.py` (new): parse the workflow with `yaml` and
  assert the `implement` job's Claude steps are build → review → finalise in that order,
  that every `.github/claude/*.md` path named in any prompt exists on disk, and that
  every step after `built` is guarded by its output. This catches the failure that costs
  a whole run: renaming an instruction file and leaving a prompt pointing at the old path.
- `docs/plans/README.md`: extend the lifecycle so step 3 describes build, review, push.

Acceptance criteria:

- `make check` and `uv run pytest` are green; the new test is in the run.
- Deleting the `Review pass 1` step, or misspelling an instruction file path in any
  prompt, fails `test/test_claude_automation_workflow.py`.
- No `git push` or `gh pr create` remains in `implement-issue.md`.
- A `workflow_dispatch` run against a real issue produces one pull request whose branch
  has a build commit followed by either a review commit or nothing, and whose body quotes
  gate results taken after the review.
- An issue with nothing to build still ends the job cleanly: the review and finalise
  steps are skipped, not failed.

## PR 2: A second review pass on a different model
**Estimated added lines:** ~80
**Files:** `.github/workflows/claude-issues.yml`, `.github/claude/review-changes.md`,
`test/test_claude_automation_workflow.py`, `docs/plans/README.md`

Additive on top of a working pipeline, so it is safe to merge on its own: if pass 2
turns out to be noise, reverting this section leaves PR 1's pipeline intact.

- `claude-issues.yml`: add `Review pass 2` between `Review pass 1` and the finalise step,
  with `--model claude-sonnet-5`, `--max-turns 40` and `REVIEW_PASS: 2`, under the same
  `steps.built` guard. Two independent models miss different things, and these runs share
  a subscription's usage windows (see the workflow header), so the second sweep should not
  cost a second opus run. The model is one line, so switching it back is one line.
- `review-changes.md`: give each pass a charter, selected by `REVIEW_PASS`. Pass 1 is
  correctness — wrong behaviour, missing or wrong tests, unhandled cases, migration
  safety, and `AGENTS.md`'s "no defensive programming". Pass 2 is fit — code that
  duplicates an existing helper, naming, scope creep beyond the plan section, the size
  budget, and documentation that the change made wrong. Pass 2 also states in its
  handover what pass 1 changed, so the finalise body can report both.
- Extend `test_claude_automation_workflow.py`: both passes use `review-changes.md`, their
  `--model` values differ, their `REVIEW_PASS` values are `1` and `2`, and pass 2 sits
  between pass 1 and the finalise step.
- `docs/plans/README.md`: one sentence that there are two review passes.

Acceptance criteria:

- `make check` and `uv run pytest` are green.
- Setting both passes to the same model fails the test that asserts they differ.
- A `workflow_dispatch` run shows both review steps executing on the same branch, and the
  pull request body distinguishes what each pass changed.
- Deleting only PR 2's workflow step leaves PR 1's pipeline green.
