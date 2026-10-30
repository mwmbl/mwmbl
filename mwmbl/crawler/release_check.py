"""Fail a pull request that changes what the crawler image runs without raising CRAWLER_VERSION.

Bumping the version on main is what publishes a crawler release (crawler-release.yml), so a
crawler change that forgets the bump would never reach volunteers on :stable.

Run as `python -m mwmbl.crawler.release_check <base-ref>`. The crawler's code is taken to be
every mwmbl module loaded by importing mwmbl.crawl, which is the image's entry point, the
data files those modules read, and the other inputs to the image build. A module the crawler
only imports inside a function is not seen.

The crawler imports the shared settings, so a setting only the server reads still counts as
a crawler change. A commit whose message has NO_CRAWLER_CHANGES on a line of its own is left
out, so it needs no release. Only that commit is: a later one touching the same files still
counts.

`python -m mwmbl.crawler.release_check --commit-msg <message-file>` is the same check for a
commit about to be made, run by pre-commit's commit-msg hook, so a commit that needs the
line or a version bump is caught before it is made rather than amended afterwards.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
VERSION_PATH = "mwmbl/crawler/version.py"
IMAGE_BUILD_INPUTS = ("Dockerfile.crawler", "pyproject.toml", "uv.lock")
# The Rust extension, and data read by loaded modules: rank.py and completer.py load
# mwmbl/resources/, justext/utils.py loads the stoplists.
CRAWLER_DIRECTORIES = ("mwmbl_rank/", "mwmbl/resources/", "mwmbl/justext/stoplists/")
VERSION_PATTERN = re.compile(r'^CRAWLER_VERSION: str = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
NO_CRAWLER_CHANGES = "No functional changes to the crawler."
# What the commit-msg hook compares the version with: where the branch will be merged.
DEFAULT_BRANCH = "origin/main"

# Importing mwmbl.crawl creates its ~400MB index under $HOME/.mwmbl, so it runs in a child
# process with a throwaway HOME. It also prints to stdout, hence the output file; its stderr
# is left alone so an import error shows in the CI log.
LIST_MODULES = """
import json, sys
import mwmbl.crawl
files = [module.__file__ for name, module in sys.modules.items()
         if name.startswith("mwmbl") and getattr(module, "__file__", None)]
with open(sys.argv[1], "w") as output:
    json.dump(files, output)
"""


def crawler_source_files() -> set[str]:
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    with tempfile.TemporaryDirectory() as home:
        output_path = Path(home) / "modules.json"
        subprocess.run(
            [sys.executable, "-c", LIST_MODULES, str(output_path)],
            env={**environment, "HOME": home},
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        module_paths = [Path(path) for path in json.loads(output_path.read_text())]
    return {str(path.relative_to(REPO_ROOT)) for path in module_paths if path.is_relative_to(REPO_ROOT)}


def parse_version(version_source: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.search(version_source)
    assert match is not None, f"No X.Y.Z CRAWLER_VERSION in {VERSION_PATH}"
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def declares_no_crawler_changes(message: str) -> bool:
    return any(line.strip() == NO_CRAWLER_CHANGES for line in message.splitlines())


def could_be_crawler_files(changed_files: list[str]) -> bool:
    """Whether any of these could be crawler files, without the slow import that says which are."""
    return any(
        (path.startswith("mwmbl/") and path.endswith(".py"))
        or path in IMAGE_BUILD_INPUTS
        or path.startswith(CRAWLER_DIRECTORIES)
        for path in changed_files
    )


def changed_crawler_files(changed_files: list[str], crawler_files: set[str]) -> list[str]:
    return [
        path
        for path in changed_files
        if path != VERSION_PATH
        and (path in crawler_files or path in IMAGE_BUILD_INPUTS or path.startswith(CRAWLER_DIRECTORIES))
    ]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout


def files_changed_since(base_ref: str) -> list[str]:
    """The files changed by the commits since base_ref, but for those declaring no crawler changes."""
    changed_files = set()
    for commit in _git("rev-list", f"{base_ref}..HEAD").split():
        message = _git("show", "-s", "--format=%B", commit)
        if declares_no_crawler_changes(message):
            print(f"Skipping {commit[:12]}, which declares no functional changes to the crawler")
            continue
        # --cc lists a merge's own changes - its conflict resolutions - rather than everything
        # it brought in from one side, which for the merge CI checks out is the whole PR.
        changed_files.update(_git("diff-tree", "--no-commit-id", "--name-only", "-r", "--cc", commit).splitlines())
    return sorted(changed_files)


def main(base_ref: str) -> int:
    changed_files = files_changed_since(base_ref)
    crawler_changes = changed_crawler_files(changed_files, crawler_source_files())
    if not crawler_changes:
        print("No crawler code changed")
        return 0

    base_version = parse_version(_git("show", f"{base_ref}:{VERSION_PATH}"))
    head_version = parse_version((REPO_ROOT / VERSION_PATH).read_text())
    if head_version > base_version:
        print(f"Crawler code changed and CRAWLER_VERSION was raised to {'.'.join(map(str, head_version))}")
        return 0

    print(f"::error::These crawler files changed, so raise CRAWLER_VERSION in {VERSION_PATH}:")
    _print_crawler_changes(crawler_changes)
    print("To roll back a release, ship the old code as a new, higher version: a version that is")
    print("already tagged is never published again.")
    return 1


def check_commit_message(message_path: str) -> int:
    """Check the commit about to be made, the cheap checks first, since the import takes seconds."""
    if declares_no_crawler_changes(Path(message_path).read_text()):
        return 0
    staged_files = _git("diff", "--cached", "--name-only").splitlines()
    if not could_be_crawler_files(staged_files):
        return 0
    merge_base = _git("merge-base", "HEAD", DEFAULT_BRANCH).strip()
    base_version = parse_version(_git("show", f"{merge_base}:{VERSION_PATH}"))
    staged_version = parse_version(_git("show", f":{VERSION_PATH}"))
    if staged_version > base_version:
        return 0
    crawler_changes = changed_crawler_files(staged_files, crawler_source_files())
    if not crawler_changes:
        return 0

    print(f"This commit changes crawler files, but CRAWLER_VERSION is not raised above {DEFAULT_BRANCH}'s:")
    _print_crawler_changes(crawler_changes)
    print(f"Raise it in {VERSION_PATH}, or if the crawler's behaviour is unchanged - a setting only")
    print("the server reads, say - add this line to the commit message:")
    print(f"  {NO_CRAWLER_CHANGES}")
    return 1


def _print_crawler_changes(crawler_changes: list[str]) -> None:
    for path in crawler_changes:
        print(f"  {path}")


if __name__ == "__main__":
    if sys.argv[1] == "--commit-msg":
        sys.exit(check_commit_message(sys.argv[2]))
    sys.exit(main(sys.argv[1]))
