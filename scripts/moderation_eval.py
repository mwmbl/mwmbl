#!/usr/bin/env python3
"""Reproduce the domain moderation model's offline numbers without a production database.

The management command trains from DomainSubmission; this trains the same way from the
sanitized judgments export, so the evaluation is reproducible on a laptop and in CI.

    uv run python scripts/moderation_eval.py
    uv run python scripts/moderation_eval.py --derived 2000     # needs network

Reports every metric with a bootstrap interval. That is not decoration: the cold-start slice
has ~80 positives, and the augmentation experiment that motivated the derived-data design
produced a 0.04 PR-AUC "gain" whose interval spanned 0.17.
"""
import argparse
import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")

import django

django.setup()

from mwmbl.moderation.train import train                          # noqa: E402
from mwmbl.moderation.training_data import (                      # noqa: E402
    REAL, TrainingRow, is_trainable_domain, derived_rows, seed_rows)

DEFAULT_EXPORT = Path("devdata/judgments_export/domains.jsonl.gz")


def rows_from_export(path: Path) -> list[TrainingRow]:
    rows = []
    for line in gzip.open(path, "rt"):
        record = json.loads(line)
        if record["status"] not in ("APPROVED", "REJECTED"):
            continue
        if not is_trainable_domain(record["domain"]):
            continue
        rejected = record["status"] == "REJECTED"
        rows.append(TrainingRow(
            domain=record["domain"],
            rejected=rejected,
            reason=record.get("rejection_reason") or "" if rejected else "",
            source=REAL,
            # The export carries no page text: it predates the evidence crawl. So this
            # measures the domain-name-only floor, which is what the model falls back to for
            # any submission whose crawl failed.
            page_texts=[],
            timestamp=record["timestamp"],
            submitter=record.get("user"),
        ))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--derived", type=int, default=0,
                        help="Blocklist rows per under-represented reason (0 = skip the fetch)")
    parser.add_argument("--no-seed", action="store_true")
    options = parser.parse_args()

    if not options.export.exists():
        print(f"No export at {options.export}", file=sys.stderr)
        return 1

    rows = rows_from_export(options.export)
    print(f"real rows: {len(rows)} "
          f"({sum(row.rejected for row in rows)} rejected)")

    if not options.no_seed:
        rows += seed_rows()
    if options.derived:
        rows += derived_rows(["OFFENSIVE"], options.derived,
                             exclude={row.domain for row in rows})

    _, metrics = train(rows, "offline-eval")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
