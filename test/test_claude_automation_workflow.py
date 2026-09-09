"""The issue automation is one Claude session wired into GitHub, and a run costs real money.

Almost all of it is prompt: the run decides what the issue needs, has the change reviewed
by a subagent, pushes, and opens or comments on the pull request itself. Only two pieces of
shell survive, on either side of it — the selector that decides an issue is worth a run at
all, and the classifier that says how the job ended.

That leaves very little that can be checked mechanically, and makes the little there is
worth checking: a renamed instruction file, a marker that drifts out of step with the
selector, or a guard dropped from the trigger list all look fine until a run burns its
budget or wakes itself in a loop. These tests are cheap and they run in CI, which is where
that feedback belongs. What the run does with its instructions is not testable here, and
deliberately not faked: this file checks the wiring, not the judgement.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/claude-issues.yml"
SELECTOR_PATH = REPOSITORY_ROOT / ".github/scripts/select-claude-work.sh"
RUN_INSTRUCTIONS_PATH = REPOSITORY_ROOT / ".github/claude/run.md"
CLAUDE_ACTION = "anthropics/claude-code-action@v1"

# The marker that says a run had its turn at the current commit and changed nothing, and
# the prefix that says a comment is the automation's whatever else it means.
TURN_TAKEN_MARKER = "<!-- claude-run: done -->"
MARKER_PREFIX = "<!-- claude-run:"

# yaml.safe_load reads the `on:` key as the boolean True, per YAML 1.1.
TRIGGERS_KEY = True


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


@pytest.fixture(scope="module")
def work_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["work"]["steps"]


@pytest.fixture(scope="module")
def claude_steps(work_steps: list[dict]) -> list[dict]:
    return [step for step in work_steps if step.get("uses") == CLAUDE_ACTION]


@pytest.fixture(scope="module")
def run_instructions() -> str:
    return RUN_INSTRUCTIONS_PATH.read_text()


def test_the_work_is_one_claude_session(claude_steps: list[dict]) -> None:
    """A second step would be a second cold context that has to be told what the first one
    did, and the whole point of this shape is that there is nothing to hand over."""
    assert [step["id"] for step in claude_steps] == ["run"]


def test_the_run_can_spawn_the_review_subagent(claude_steps: list[dict]) -> None:
    """The review is the only thing standing between a change and a human, and it is a
    subagent now. A tool name that matches nothing is inert: the run would carry on and
    push, having reviewed its own work."""
    allowed = claude_steps[0]["with"]["claude_args"]
    assert re.search(r"--allowedTools\s+\"[^\"]*\bAgent\b", allowed)
    assert ".github/claude/review-changes.md" in RUN_INSTRUCTIONS_PATH.read_text()


def test_every_instruction_file_a_prompt_names_exists(claude_steps: list[dict]) -> None:
    """A renamed instruction file is invisible until a run reads the prompt and finds
    nothing there, an hour and a full model budget later."""
    named = [
        path for step in claude_steps for path in re.findall(r"\.github/claude/[\w-]+\.md", step["with"]["prompt"])
    ]
    assert named, "no instruction file is named in any prompt"
    for path in named:
        assert (REPOSITORY_ROOT / path).is_file(), path


def test_instruction_files_only_cross_reference_files_that_exist() -> None:
    instruction_files = sorted((REPOSITORY_ROOT / ".github/claude").glob("*.md"))
    assert instruction_files
    for instruction_file in instruction_files:
        for path in re.findall(r"\.github/claude/[\w-]+\.md", instruction_file.read_text()):
            assert (REPOSITORY_ROOT / path).is_file(), f"{instruction_file.name} -> {path}"


def test_instruction_files_only_cross_reference_sections_that_exist() -> None:
    """The filename guard above does not catch a section that moved. Sending a run back to
    the section that reviews the change when it wanted the one that pushes is how a push
    ends up skipping the rules that bound the retries."""
    for instruction_file in sorted((REPOSITORY_ROOT / ".github/claude").glob("*.md")):
        for block in re.split(r"\n\s*\n", instruction_file.read_text()):
            sections = re.findall(r"\bsections? (\d+)(?:\s+and\s+(\d+))?", block)
            if not sections:
                continue
            # A block that names no file is talking about itself.
            named = set(re.findall(r"\.github/claude/[\w-]+\.md", block))
            assert len(named) <= 1, f"{instruction_file.name}: ambiguous target {named}"
            target = REPOSITORY_ROOT / named.pop() if named else instruction_file
            headings = re.findall(r"^## (\d+)\.", target.read_text(), re.MULTILINE)
            for number in {n for pair in sections for n in pair if n}:
                assert number in headings, f"{instruction_file.name} -> {target.name} #{number}"


def test_the_run_is_told_to_have_the_change_reviewed_before_it_pushes(run_instructions: str) -> None:
    """Nothing enforces this any more, so the ordering has to be in the instructions and
    has to stay there: a review after the push is a review a human has already seen past."""
    review_section = run_instructions.index("## 4. Have the change reviewed")
    push_section = run_instructions.index("## 5. Push it")
    assert review_section < push_section


def test_the_review_subagent_is_given_nothing_but_the_issue_number(run_instructions: str) -> None:
    """Everything else the run could add is the author reviewing their own work through a
    second context. The prompt it is told to send is fixed text for that reason."""
    spawn_prompt = run_instructions.split("## 4. Have the change reviewed")[1].split("```")[1]
    assert spawn_prompt.strip().splitlines() == [
        "Follow the instructions in .github/claude/review-changes.md exactly.",
        "",
        "ISSUE_NUMBER: $N",
    ]


def test_every_claude_step_is_recorded_so_a_failure_can_be_classified(
    work_steps: list[dict],
) -> None:
    """classify-claude-run.sh can only tell a spent usage window from a real failure for
    steps that made it into the log, and an unrecorded failure would end the job green."""
    for index, step in enumerate(work_steps):
        if step.get("uses") != CLAUDE_ACTION:
            continue
        recorder = work_steps[index + 1]
        assert recorder["run"].split() == [
            ".github/scripts/record-claude-run.sh",
            step["id"],
        ], step["id"]
        assert recorder["if"] == "always()", step["id"]
        assert recorder["env"]["OUTCOME"] == f"${{{{ steps.{step['id']}.outcome }}}}"


def test_a_failing_claude_step_does_not_end_the_job(claude_steps: list[dict]) -> None:
    """The classifier is the last step, so it only ever runs if the red ones let it."""
    for step in claude_steps:
        assert step["continue-on-error"] is True, step["id"]


def test_the_classifier_has_the_last_word(work_steps: list[dict]) -> None:
    last = work_steps[-1]
    assert last["run"] == ".github/scripts/classify-claude-run.sh"
    assert last["if"] == "always()"


def test_no_step_between_the_run_and_the_classifier_judges_the_change(work_steps: list[dict]) -> None:
    """A step that re-ran the gates or re-read the diff would be checking a session that is
    already reporting on itself, and the branch is pushed by then either way. CI on the
    pushed branch is what catches a change the run got wrong."""
    run_index = next(index for index, step in enumerate(work_steps) if step.get("id") == "run")
    after = work_steps[run_index + 1 :]
    scripts = {step["run"].split()[0] for step in after if step.get("run", "").startswith(".github/")}
    assert scripts == {
        ".github/scripts/record-claude-run.sh",
        ".github/scripts/classify-claude-run.sh",
    }


def test_the_run_pushes_as_the_app_rather_than_with_the_workflow_token(claude_steps: list[dict]) -> None:
    """A push made with GITHUB_TOKEN triggers no workflows, so the branch would sit with no
    checks on it forever — and CI going red is the only thing that tells the selector a
    change was wrong. Passing no github_token is what makes the action authenticate as the
    Claude GitHub App instead."""
    assert "github_token" not in claude_steps[0]["with"]


def test_triggers_are_events_the_action_accepts(workflow: dict) -> None:
    """claude-code-action fails the job outright on an event outside this set, which is
    what `push` did to every merge to main between a5eb9e3 and 141ae84."""
    supported = {
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_target",
        "pull_request_review",
        "pull_request_review_comment",
        "workflow_dispatch",
        "repository_dispatch",
        "schedule",
        "workflow_run",
    }
    assert set(workflow[TRIGGERS_KEY]) <= supported


def test_ci_going_red_wakes_a_run(workflow: dict) -> None:
    """It is the only check on a change now that nothing between the run and the classifier
    reads the diff."""
    assert workflow[TRIGGERS_KEY]["workflow_run"]["workflows"] == ["CI"]


def test_a_scheduled_run_still_exists_to_retry_a_spent_usage_window(workflow: dict) -> None:
    """It is the only thing that picks an item back up after the subscription runs out."""
    assert workflow[TRIGGERS_KEY]["schedule"]


def test_comment_and_review_triggers_require_write_access(workflow: dict) -> None:
    """On a public repository, anyone can comment. Nobody without write access should be
    able to start a run that holds a repository token."""
    guard = workflow["jobs"]["select"]["if"]
    for event in ("issue_comment", "pull_request_review", "pull_request_review_comment"):
        clause = guard.split(f"github.event_name != '{event}'")[1]
        assert '"OWNER","MEMBER","COLLABORATOR"' in clause.split("&& (github.event_name")[0]


def test_no_feedback_trigger_can_be_fired_by_a_bot(workflow: dict) -> None:
    """Every comment a run posts is posted as the Claude app, which raises an event the way
    a person's comment does. author_association does not reliably identify an app, so this
    is the clause that stops the automation waking itself in a loop."""
    guard = workflow["jobs"]["select"]["if"]
    for event, field in [
        ("issue_comment", "comment"),
        ("pull_request_review", "review"),
        ("pull_request_review_comment", "comment"),
    ]:
        clause = guard.split(f"github.event_name != '{event}'")[1].split("&& (github.event_name")[0]
        assert f"!endsWith(github.event.{field}.user.login, '[bot]')" in clause, event


def test_nothing_the_run_writes_lands_inside_the_checkout(work_steps: list[dict]) -> None:
    """The run holds an unrestricted Bash tool and is asked to commit; an untracked file in
    the working tree is one `git add -A` from the diff."""
    exported = [step for step in work_steps if "GITHUB_ENV" in step.get("run", "")]
    assert exported
    for step in exported:
        for name, value in re.findall(r'echo "(CLAUDE_\w+)=(\S+)"', step["run"]):
            assert value.startswith("$RUNNER_TEMP/"), name


def test_no_checkout_lands_on_the_pull_request_merge_ref(workflow: dict) -> None:
    """`pull_request` hands github.ref as refs/pull/<n>/merge, which GitHub stops
    guaranteeing once the pull request closes — and which is not main even when it
    resolves."""
    checkouts = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts
    for checkout in checkouts:
        assert "github.event_name == 'pull_request'" in checkout["with"]["ref"]


def test_only_pull_requests_from_this_repository_start_a_run(workflow: dict) -> None:
    """A fork's pull_request event gets no secrets and a read-only token whatever the
    permissions block says, so every step of the run would fail in public."""
    guard = workflow["jobs"]["select"]["if"]
    for event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        clause = guard.split(f"github.event_name != '{event}'")[1].split("&& (github.event_name")[0]
        assert "head.repo.full_name == github.repository" in clause, event


def test_the_selector_reads_the_markers_the_run_writes() -> None:
    """Two files, two strings: if they drift apart the retries stop being bounded and
    nothing says so."""
    selector = SELECTOR_PATH.read_text()
    instructions = RUN_INSTRUCTIONS_PATH.read_text()
    for marker in (TURN_TAKEN_MARKER, MARKER_PREFIX):
        assert marker in selector, marker
        assert marker in instructions, marker


def test_a_run_that_changes_nothing_says_so_on_the_pull_request(run_instructions: str) -> None:
    """It is the only record such a run leaves. Without it the same red checks select the
    same pull request on every trigger and every scheduled run, for as long as they are
    red."""
    committed_nothing = run_instructions.split("**If you committed nothing**")[1].split("**If you committed**")[0]
    assert "comment on the pull request" in committed_nothing


def test_a_push_is_not_recorded_as_a_turn_taken(run_instructions: str) -> None:
    """The commit a run just pushed has not had its turn: CI has not run on it. Marking it
    as spoken for silences the failing checks of exactly the commits the automation made,
    so the comment that goes with a push carries the other marker."""
    assert "<!-- claude-run: update -->" in run_instructions
    assert TURN_TAKEN_MARKER != "<!-- claude-run: update -->"
    assert "<!-- claude-run: update -->" not in SELECTOR_PATH.read_text()


def test_the_selector_recognises_its_own_voice_by_marker_not_by_author() -> None:
    """gh's projection of a comment author carries a login and nothing else, and a Bot
    actor's login arrives there without the "[bot]" suffix, so an authorship test in the
    selector silently matches nothing and the automation reads its own comments as a
    maintainer's."""
    selector = SELECTOR_PATH.read_text()
    assert "def ours:" in selector
    assert 'endswith("[bot]")' not in selector
    assert "is_bot" not in selector


def test_forks_are_not_the_automations_to_work_on() -> None:
    """A fork's branch may be named claude/issue-N-anything, and is code nobody here has
    reviewed."""
    assert "isCrossRepository" in SELECTOR_PATH.read_text()


def test_a_run_cannot_outlive_the_app_token_it_pushes_with(workflow: dict) -> None:
    """claude-code-action mints a GitHub App installation token when the step starts, and
    GitHub expires those after an hour. The run pushes at the end of that same step, so a
    job allowed to run longer than the token lives can spend its whole budget and then fail
    to publish any of it — and a discarded run looks like a stuck issue, not a dead token.
    Every work job measured so far finished well inside 20 minutes."""
    timeout = workflow["jobs"]["work"]["timeout-minutes"]
    assert timeout < 60, timeout


def test_the_gates_environment_reaches_the_cli_and_not_only_the_shell(workflow: dict, work_steps: list[dict]) -> None:
    """A composite action's steps do not inherit the calling job's env, and
    claude-code-action forwards only the variables it names — DATABASE_URL is not one of
    them. Set at job level it reaches every plain step and never the CLI, so the run's own
    `uv run pytest` fails for want of a database while everything around it looks right."""
    required = {"DATABASE_URL", "DJANGO_SETTINGS_MODULE"}
    job_level = set(workflow["jobs"]["work"].get("env", {}))
    assert not (required & job_level), required & job_level
    exported = "".join(step.get("run", "") for step in work_steps if "GITHUB_ENV" in step.get("run", ""))
    for name in required:
        assert f"{name}=" in exported, name


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq drives the selector's filter")
@pytest.mark.parametrize(
    "body,is_ours,is_turn",
    [
        ("Nothing changed.\n\n<!-- claude-run: done -->", True, True),
        ("Pushed a fix.\n\n<!-- claude-run: update -->", True, False),
        ("> earlier\n> <!-- claude-run: done -->\n\nBut what about X?", False, False),
        ("What about X?\n\n> earlier\n> <!-- claude-run: done -->", False, False),
    ],
)
def test_a_quoted_marker_is_not_the_automations_own_voice(body: str, is_ours: bool, is_turn: bool) -> None:
    """GitHub's "Quote reply" copies the quoted comment's raw markdown, HTML comments
    included. Read as the automation's own voice, a maintainer's reply is dropped from the
    feedback that wakes a run — the exact case the markers exist to unblock — and counts as
    a turn taken, silencing the failing checks on that commit."""
    selector = SELECTOR_PATH.read_text()
    patterns = dict(re.findall(r"^readonly (marker|turn_taken)='(.+)'$", selector, re.MULTILINE))
    assert patterns.keys() == {"marker", "turn_taken"}, patterns

    def matches(pattern: str) -> bool:
        result = subprocess.run(
            ["jq", "-nr", "--arg", "b", body, "--arg", "rx", pattern, "$b | test($rx)"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    assert matches(patterns["marker"]) is is_ours
    assert matches(patterns["turn_taken"]) is is_turn


def test_referenced_scripts_are_executable(work_steps: list[dict], workflow: dict) -> None:
    steps = work_steps + workflow["jobs"]["select"]["steps"]
    scripts = {step["run"].split()[0] for step in steps if step.get("run", "").startswith(".github/")}
    assert scripts
    for script in scripts:
        path = REPOSITORY_ROOT / script
        assert path.is_file(), script
        assert os.access(path, os.X_OK), f"{script} is not executable"
