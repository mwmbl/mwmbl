#!/usr/bin/env python3
"""Reproduce the domain moderation model's offline numbers without a production database.

The management command trains from DomainSubmission; this trains the same way from the
sanitized judgments export, so the evaluation is reproducible on a laptop and in CI.

    uv run python scripts/moderation_eval.py
    uv run python scripts/moderation_eval.py --derived 2000     # needs network
    uv run python scripts/moderation_eval.py --write-artifact   # regenerate the warm start

Reports every metric with a bootstrap interval. That is not decoration: the cold-start slice
has ~80 positives, and the augmentation experiment that motivated the derived-data design
produced a 0.04 PR-AUC "gain" whose interval spanned 0.17.
"""
import argparse
import gzip
import json
import os
import sys
from datetime import date
from pathlib import Path

import joblib

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")

import django

django.setup()

from mwmbl.moderation.train import train                          # noqa: E402
from mwmbl.moderation.model import METRICS_FILENAME, MODEL_FILENAME  # noqa: E402
from mwmbl.moderation.training_data import (                      # noqa: E402
    REAL, TrainingRow, is_trainable_domain, derived_rows, seed_rows)

DEFAULT_EXPORT = Path("devdata/judgments_export/domains.jsonl.gz")
ARTIFACT_DIR = Path("mwmbl/moderation/artifacts")


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
    parser.add_argument(
        "--write-artifact", action="store_true",
        help=(f"Overwrite the committed warm start in {ARTIFACT_DIR}. Needed after a change to "
              "the feature set: a featuriser is pickled whole, so the shipped artifact then "
              "describes a matrix the code no longer builds, fails the load-time probe, and "
              "every deploy degrades to the deterministic checks until the first retrain."))
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

    # Dated like a retrain's, because this artifact is served in production until the first
    # retrain publishes and "offline-eval" in a worker log says nothing about how old it is -
    # but suffixed, because it is *not* one. DomainEvidence.model_version records which model
    # scored a row, and a warm start sharing a retrain's version string makes "has the rescore
    # run yet?" unanswerable from the data.
    version = (f"domain-mod-{date.today():%Y-%m-%d}-warmstart" if options.write_artifact
               else "offline-eval")
    model, metrics = train(rows, version)
    print(json.dumps(metrics, indent=2))

    if options.write_artifact:
        joblib.dump(model, ARTIFACT_DIR / MODEL_FILENAME)
        (ARTIFACT_DIR / METRICS_FILENAME).write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"\nWrote the warm start to {ARTIFACT_DIR}. This is the day-one model only - a "
              f"retrain against the production database still replaces it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
