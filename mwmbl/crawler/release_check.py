"""Fail a pull request that changes what the crawler image runs without raising CRAWLER_VERSION.

Bumping the version on main is what publishes a crawler release (crawler-release.yml), so a
crawler change that forgets the bump would never reach volunteers on :stable.

Run as `python -m mwmbl.crawler.release_check <base-ref>`. The crawler's code is taken to be
every mwmbl module loaded by importing mwmbl.crawl, which is the image's entry point, the
data files those modules read, and the other inputs to the image build. A module the crawler
only imports inside a function is not seen.
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

# Importing mwmbl.crawl creates the external cache under $HOME/.mwmbl, so it runs in a child
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


def changed_crawler_files(changed_files: list[str], crawler_files: set[str]) -> list[str]:
    return [
        path
        for path in changed_files
        if path != VERSION_PATH
        and (path in crawler_files or path in IMAGE_BUILD_INPUTS or path.startswith(CRAWLER_DIRECTORIES))
    ]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout


def main(base_ref: str) -> int:
    changed_files = _git("diff", "--name-only", base_ref, "HEAD").splitlines()
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
    for path in crawler_changes:
        print(f"  {path}")
    print("To roll back a release, ship the old code as a new, higher version: a version that is")
    print("already tagged is never published again.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
