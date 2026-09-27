"""Merge the judges' output into `devdata/combined_ltr_labels/pass3_serving_pool.jsonl`.

Each judge writes `$WORK/out_NN.txt`, one `<id> || <relevance> || <ethos> || <overall>` line
per candidate. Every id in the manifest must be graded exactly once, in range; anything else
is an error, so a misaligned or truncated output never slips in.

New pairs are written as Pass-3 rows. Anchors are written too, flagged `anchor: true`, and
their grades are compared with the originals in `pass3_judgments.jsonl` to report drift.
"""

import json
import os
import re
from pathlib import Path

import numpy as np

WORK = Path(os.environ.get("HAIKU_WORK_DIR", "devdata/combined_ltr_labels/haiku_work"))
ORIGINAL = Path("devdata/llm_relabel/pass3_judgments.jsonl")
OUT = Path("devdata/combined_ltr_labels/pass3_serving_pool.jsonl")
LINE = re.compile(r"^\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*$")


def run():
    manifest = {int(k): v for k, v in json.loads((WORK / "manifest.json").read_text()).items()}
    grades: dict[int, tuple[int, int, int]] = {}
    for path in sorted(WORK.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            match = LINE.match(line)
            if not match:
                continue
            cid, relevance, ethos, overall = map(int, match.groups())
            assert cid in manifest, f"{path.name}: unknown id {cid}"
            assert cid not in grades, f"{path.name}: id {cid} graded twice"
            assert relevance <= 3 and ethos <= 3 and overall <= 10, f"{path.name}: out of range: {line}"
            grades[cid] = (relevance, ethos, overall)
    missing = sorted(set(manifest) - set(grades))
    assert not missing, f"{len(missing)} ids ungraded, first {missing[:10]}"

    original = {(j["query"], j["url"]): j for j in map(json.loads, open(ORIGINAL))}
    drift = []
    with open(OUT, "w") as f:
        for cid in sorted(manifest):
            query, url, is_anchor = manifest[cid]
            relevance, ethos, overall = grades[cid]
            record = {"query": query, "url": url, "relevance": relevance, "ethos": ethos, "overall": overall}
            if is_anchor:
                record["anchor"] = True
                before = original[(query, url)]
                drift.append([overall - before["overall"], relevance - before["relevance"], ethos - before["ethos"]])
            f.write(json.dumps(record) + "\n")

    new = [g for cid, g in grades.items() if not manifest[cid][2]]
    print(f"{len(new)} new judgments, {len(drift)} anchors -> {OUT}")
    print("new: mean relevance %.2f, ethos %.2f, overall %.2f" % tuple(np.mean(new, axis=0)))
    d = np.array(drift)
    print("anchor drift (new - original): overall %+.2f, relevance %+.2f, ethos %+.2f" % tuple(d.mean(axis=0)))
    print(
        "anchor overall within 1: %.0f%%, exact: %.0f%%"
        % (100 * np.mean(abs(d[:, 0]) <= 1), 100 * np.mean(d[:, 0] == 0))
    )


if __name__ == "__main__":
    run()
