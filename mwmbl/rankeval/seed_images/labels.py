"""Check the judges' out_<batch>.txt files and join them to their manifests, as gate/labels_<kind>.jsonl.

Fails loudly, naming the batch, on a malformed line, an unknown or repeated id, a score outside 0-3 or an item
left unlabelled, so a bad batch is sent back to its judge rather than silently dropped.
"""

import json
from pathlib import Path

from mwmbl.rankeval.seed_images.gate_data import GATE_DIR
from mwmbl.rankeval.seed_images.queries import read_jsonl

# kind: (batch name prefix, label field, other required string fields)
KINDS = {
    "need": ("need", "need", ("reason",)),
    "relevance": ("rel", "relevance", ()),
    "vision": ("vision", "relevance", ()),
}
LEVELS = range(4)


def parse_out(text: str, expected_ids: set[str], field: str, string_fields: tuple[str, ...]) -> dict[str, dict]:
    labels = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        assert row["id"] in expected_ids, f"unexpected id {row['id']}"
        assert row["id"] not in labels, f"id {row['id']} labelled twice"
        assert row[field] in LEVELS and isinstance(row[field], int), f"{row['id']}: {field} {row[field]!r} not 0-3"
        assert all(isinstance(row[name], str) for name in string_fields), f"{row['id']}: missing {string_fields}"
        labels[row["id"]] = {field: row[field]} | {name: row[name] for name in string_fields}
    missing = expected_ids - set(labels)
    assert not missing, f"{len(missing)} items unlabelled, e.g. {sorted(missing)[:3]}"
    return labels


def batch_ids(batch_path: Path) -> set[str]:
    return {line.split()[1] for line in batch_path.read_text().splitlines() if line.startswith("### ")}


def consolidate(kind: str) -> list[dict]:
    prefix, field, string_fields = KINDS[kind]
    manifest = {row["id"]: row for row in read_jsonl(GATE_DIR / f"manifest_{kind}.jsonl")}
    labels = {}
    for batch_path in sorted(GATE_DIR.glob(f"batch_{prefix}_*.txt")):
        out_path = GATE_DIR / batch_path.name.replace("batch_", "out_", 1)
        try:
            labels |= parse_out(out_path.read_text(), batch_ids(batch_path), field, string_fields)
        except (AssertionError, KeyError, ValueError, FileNotFoundError) as error:
            raise ValueError(f"{out_path.name}: {error}") from error
    assert set(labels) == set(manifest), f"{kind}: batches do not cover the manifest"
    return [manifest[item_id] | labels[item_id] for item_id in sorted(labels)]


def main() -> None:
    for kind in KINDS:
        rows = consolidate(kind)
        (GATE_DIR / f"labels_{kind}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        print(f"labels_{kind}.jsonl: {len(rows)} labels")


if __name__ == "__main__":
    main()
