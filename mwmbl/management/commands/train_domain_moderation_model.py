"""Train the domain moderation suggester and, if it passes the gate, publish the artifact.

    uv run manage.py train_domain_moderation_model --dry-run
    uv run manage.py train_domain_moderation_model

The gate compares the new model's cold-start PR-AUC against the lower bound of the incumbent's
bootstrap interval, so a change that only looks like an improvement does not ship.

Publishing writes a ModerationModelArtifact row rather than a file: the incumbent the gate
reads and the model the workers serve are then the same object, and neither is lost to a
deploy. See mwmbl.moderation.model.
"""
import json
from datetime import date

from django.core.management.base import BaseCommand

from mwmbl.background import rescore_pending_submissions
from mwmbl.models import DomainSubmission
from mwmbl.moderation import training_data
from mwmbl.moderation.model import load_metrics, publish
from mwmbl.moderation.train import passes_gate, train

# A reason class with fewer real examples than this is a candidate for blocklist-derived
# rows, but only when --derived is passed. See mwmbl.moderation.training_data: derived rows
# cost the classes that do have real labels more than they are worth here, and OFFENSIVE -
# the only class short of data - is served exactly by the blocklist check in rules.py instead.
MIN_REAL_ROWS_PER_REASON = 30
DERIVED_ROWS_PER_REASON = 300


class Command(BaseCommand):
    help = "Train the domain moderation approve/reject suggester"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Train and report metrics without writing the artifact")
        parser.add_argument("--force", action="store_true",
                            help="Publish even if the gate fails")
        parser.add_argument(
            "--derived", action="store_true",
            help=("Add blocklist-derived rows for reason classes short of real data. Off by "
                  "default: measured at 100 rows this drops real SPAM F1 from 0.839 to 0.688, "
                  "and OFFENSIVE is already covered exactly by the blocklist check."))

    def handle(self, *args, **options):
        rows = training_data.real_rows()
        self.stdout.write(f"Real decided submissions usable for training: {len(rows)}")
        if not rows:
            self.stderr.write("No decided submissions to train on.")
            return

        rows += training_data.seed_rows()

        if options["derived"]:
            needed = self._reasons_needing_data(rows)
            if needed:
                self.stdout.write(f"Reason classes with too little real data: {sorted(needed)}")
                rows += training_data.derived_rows(
                    needed, DERIVED_ROWS_PER_REASON,
                    exclude={row.domain for row in rows})

        version = f"domain-mod-{date.today():%Y-%m-%d}"
        model, metrics = train(rows, version)
        self.stdout.write(json.dumps(metrics, indent=2))

        allowed, explanation = passes_gate(metrics, load_metrics())
        self.stdout.write(("PASS: " if allowed else "FAIL: ") + explanation)

        if options["dry_run"]:
            self.stdout.write("Dry run - artifact not written.")
            return
        if not allowed and not options["force"]:
            self.stderr.write("Gate failed; not publishing. Re-run with --force to override.")
            return

        publish(model, metrics)
        self.stdout.write(self.style.SUCCESS(
            f"Published {model.version}; every worker picks it up within a minute."))
        rescore_pending_submissions(schedule=0)
        self.stdout.write("Scheduled a rescore of the pending queue with the new model.")

    @staticmethod
    def _reasons_needing_data(rows) -> set[str]:
        counts: dict[str, int] = {}
        for row in rows:
            if row.source == training_data.REAL and row.rejected and row.reason:
                counts[row.reason] = counts.get(row.reason, 0) + 1
        return {reason for reason in DomainSubmission.DOMAIN_REJECTION_REASON
                if counts.get(reason, 0) < MIN_REAL_ROWS_PER_REASON}
