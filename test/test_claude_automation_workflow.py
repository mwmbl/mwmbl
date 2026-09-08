"""The issue automation is a pipeline wired together in YAML, and a run costs an hour.

Nothing else checks that wiring: a renamed instruction file, a review step dropped in a
merge or a guard left off a pushing step all look fine until a run burns its budget or
opens a pull request nobody reviewed. These tests are cheap and they run in CI, which is
where that feedback belongs.
"""

import os
import re
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/claude-issues.yml"
CLAUDE_ACTION = "anthropics/claude-code-action@v1"

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


def test_the_pipeline_is_build_then_review_then_finalise(claude_steps: list[dict]) -> None:
    """Reviewing after the push, or not at all, is the failure this ordering prevents."""
    assert [step["id"] for step in claude_steps] == ["coordinate", "review", "finalise"]


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


def test_review_and_finalise_run_only_on_a_successful_build(claude_steps: list[dict]) -> None:
    """Without the guard, a build that hit its turn limit part-way through a change still
    gets pushed and opened for review."""
    for step in claude_steps[1:]:
        guard = step["if"]
        assert "steps.built.outputs.built == 'true'" in guard, step["id"]
        assert "steps.coordinate.outcome == 'success'" in guard, step["id"]


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


def test_referenced_scripts_are_executable(work_steps: list[dict], workflow: dict) -> None:
    steps = work_steps + workflow["jobs"]["select"]["steps"]
    scripts = {step["run"].split()[0] for step in steps if step.get("run", "").startswith(".github/")}
    assert scripts
    for script in scripts:
        path = REPOSITORY_ROOT / script
        assert path.is_file(), script
        assert os.access(path, os.X_OK), f"{script} is not executable"
