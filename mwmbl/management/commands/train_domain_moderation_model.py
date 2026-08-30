"""Train the domain moderation suggester and, if it passes the gate, publish the artifact.

    uv run manage.py train_domain_moderation_model --dry-run
    uv run manage.py train_domain_moderation_model

The gate scores the incumbent and the candidate on the *same* held-out rows and bootstraps the
difference, so a change that only looks like an improvement does not ship - and, just as
importantly, a change that only looks like a regression because the population moved underneath
it is not blocked. See mwmbl.moderation.train for why raw PR-AUC cannot be compared across two
runs.

    # is the crawled page text earning its place?
    uv run manage.py train_domain_moderation_model --dry-run --ablate

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
from mwmbl.moderation.model import load_metrics, load_published_model, publish
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
            "--no-text", action="store_true", dest="no_text",
            help="Fit on the domain name alone, ignoring the crawled page text vocabulary")
        parser.add_argument(
            "--has-text", action="store_true", dest="has_text",
            help=("Add the was-it-crawled indicator, which is off by default because it "
                  "measured worse on every metric: evidence is crawled newest-first, so it "
                  "doubles as a proxy for recency. Worth re-measuring with --ablate once "
                  "backfill_domain_evidence has evened out the coverage"))
        parser.add_argument(
            "--ablate", action="store_true",
            help=("Also train with the has_text indicator and without the page-text "
                  "vocabulary, and report all three on the same split, so each feature is "
                  "measured rather than guessed"))
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
        # The model the workers are serving right now, so the gate can score it on the same
        # held-out rows as the candidate. None on a first run, and on an artifact this code can
        # no longer featurise - the gate falls back to the stored metrics and says so.
        incumbent = load_published_model()
        model, metrics = train(rows, version, use_text=not options["no_text"],
                               use_has_text=options["has_text"], incumbent=incumbent)
        self.stdout.write(json.dumps(metrics, indent=2))

        if options["ablate"]:
            self._report_ablation(rows, version, metrics)

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

    def _report_ablation(self, rows, version, full: dict) -> None:
        """Hold out each half of the page-text features in turn, on the same split.

        Same rows, same seed, so each line differs from what ships by exactly one feature. Both
        ranking quality and the *operating point* are reported: a feature can hold the ranking
        while shifting the score distribution through MODERATION_REJECT_THRESHOLD, and
        normalised AP alone cannot see that.

        The intervals are independent, not paired, so they overlap far more than the difference
        between two lines is uncertain by. Read the direction across all four columns rather
        than any single interval.
        """
        _, no_text = train(rows, f"{version}-no-text", use_text=False, use_has_text=False)
        _, with_has_text = train(rows, f"{version}-has-text", use_has_text=True)

        self.stdout.write("\nAblation - are the page-text features earning their place?")
        self.stdout.write(f"  {'':>22}  {'norm AP':>17}  {'reject P':>16}  "
                          f"{'reject R':>16}  {'unsure':>7}")
        for name, metrics in (("text (shipped)", full),
                              ("text + has_text", with_has_text),
                              ("domain name only", no_text)):
            cold = metrics.get("cold_start")
            if cold is None:
                self.stdout.write(f"  {name:>22}: no cold-start slice")
                continue
            at = cold["at_thresholds"]
            self.stdout.write(
                f"  {name:>22}  {self._with_interval(cold['normalised_ap'], cold['normalised_ap_ci'])}  "
                f"{self._with_interval(at['reject_precision'], at['reject_precision_ci'])}  "
                f"{self._with_interval(at['reject_recall'], at['reject_recall_ci'])}  "
                f"{at['unsure_share']:>7.3f}")

        cold = full.get("cold_start", {})
        self.stdout.write(
            f"\n  {full.get('train_rows_with_text', 0)} of "
            f"{sum(full.get('train_rows_by_source', {}).values())} training rows and "
            f"{cold.get('rows_with_text', 0)} of {cold.get('rows', 0)} cold-start test rows "
            f"have text.\n  A large gap between those two makes has_text a proxy for recency "
            f"rather than for evidence, which is why it is off by default. Backfill evidence "
            f"for the older\n  submissions to close the gap, then re-read this table.\n")

    @staticmethod
    def _with_interval(value, interval) -> str:
        """A point estimate and its interval, or a dash where the threshold selected nothing."""
        if value is None:
            return f"{'-':>16}"
        if interval is None:
            return f"{value:>6.3f}          "
        return f"{value:>6.3f} [{interval[0]:.2f},{interval[1]:.2f}]"

    @staticmethod
    def _reasons_needing_data(rows) -> set[str]:
        counts: dict[str, int] = {}
        for row in rows:
            if row.source == training_data.REAL and row.rejected and row.reason:
                counts[row.reason] = counts.get(row.reason, 0) + 1
        return {reason for reason in DomainSubmission.DOMAIN_REJECTION_REASON
                if counts.get(reason, 0) < MIN_REAL_ROWS_PER_REASON}
