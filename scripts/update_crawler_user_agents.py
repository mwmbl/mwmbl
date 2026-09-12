"""Refresh the vendored crawler user agent patterns.

    uv run python scripts/update_crawler_user_agents.py

Source: https://github.com/monperrus/crawler-user-agents, MIT licensed, © Martin
Monperrus and contributors. The upstream file is ~540 KB of pretty-printed JSON carrying
a url, an addition date and a description for every entry; none of that is used at
request time, so this writes out only the patterns and the example instances, compacted
onto one line. The instances are kept because they are what test_traffic_classify.py
asserts against - a refresh that broke the matcher would otherwise pass silently.

Review the pattern count in the diff, not the blob: this script is the only thing that
writes the file.
"""

import json
from pathlib import Path
from urllib.request import urlopen

UPSTREAM_URL = "https://raw.githubusercontent.com/monperrus/crawler-user-agents/master/crawler-user-agents.json"
OUTPUT_PATH = Path(__file__).parent.parent / "mwmbl" / "resources" / "crawler_user_agents.json"


def main() -> None:
    with urlopen(UPSTREAM_URL) as response:
        upstream = json.load(response)

    patterns = [entry["pattern"] for entry in upstream]
    instances = sorted({instance for entry in upstream for instance in entry.get("instances", [])})

    with open(OUTPUT_PATH, "w") as output_file:
        json.dump({"patterns": patterns, "instances": instances}, output_file, separators=(",", ":"))

    print(f"Wrote {len(patterns)} patterns and {len(instances)} instances to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
