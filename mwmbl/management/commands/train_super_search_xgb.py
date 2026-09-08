"""Train the Super Search xgb source model and write its serving artifact.

Two data sources:

- ``--from-matrix PATH``  a dense offline ``RewardMatrix`` (``.npz`` +
  ``.json`` pair, e.g. ``devdata/ss_judge_matrix``) — used to build the
  repo-bundled warm-start artifact from the per-source home-query set. Also
  writes the artifact's Redis seed data: per-source reward means into
  ``meta.json`` and, from the matrix build's fetch checkpoint
  (``<matrix>.fetch.jsonl`` or ``--profiles-from``), the batch content
  profiles into ``profiles.npz`` — both SETNX-seeded at startup so serving
  features match what the model trained on (``xgb_model.seed_online_state``).
- ``--days N``            logged ``SuperSearchImpression`` rows from the last
  N days — the same path the daily background retrain takes.

The artifact directory defaults to ``settings.SUPER_SEARCH_XGB_MODEL_DIR``;
pass ``--out mwmbl/tinysearchengine/super_search_select/artifacts/xgb`` to
refresh the bundled warm-start model.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mwmbl.tinysearchengine.super_search_select import vectors, xgb_model
from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix


def profiles_from_fetch(path: Path, dim: int) -> dict:
    """Batch content profiles from a build-matrix fetch checkpoint, computed
    exactly as the matrix build computed them (L2-normalised projection sums)."""
    import numpy as np

    prof_bow: dict[str, np.ndarray] = {}
    prof_cng: dict[str, np.ndarray] = {}
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        for name, docs in rec["docs"].items():
            text = " ".join(f"{title or ''} {extract or ''}" for _, title, extract in docs)
            prof_bow.setdefault(name, np.zeros(dim))
            prof_cng.setdefault(name, np.zeros(dim))
            prof_bow[name] += vectors.project_bow(text, dim)
            prof_cng[name] += vectors.project_char_ngrams(text, dim)
    return {name: (vectors._l2_normalise(prof_bow[name]), vectors._l2_normalise(prof_cng[name])) for name in prof_bow}


class Command(BaseCommand):
    help = "Train the Super Search xgb source model (from a reward matrix or the impression log)"

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--from-matrix", metavar="PATH", help="RewardMatrix path (without extension)")
        source.add_argument("--days", type=int, help="train from SuperSearchImpression rows of the last N days")
        parser.add_argument("--min-rows", type=int, default=settings.SUPER_SEARCH_XGB_MIN_TRAIN_ROWS)
        parser.add_argument(
            "--out",
            default=settings.SUPER_SEARCH_XGB_MODEL_DIR,
            help="artifact directory (default: SUPER_SEARCH_XGB_MODEL_DIR)",
        )
        parser.add_argument(
            "--reward-kind", default="judge", help="provenance label stored in meta.json for --from-matrix"
        )
        parser.add_argument(
            "--profiles-from",
            metavar="JSONL",
            help="fetch checkpoint to build profiles.npz from (default: <matrix>.fetch.jsonl)",
        )
        parser.add_argument("--no-profiles", action="store_true", help="skip writing profiles.npz for --from-matrix")

    def handle(self, *args, **options):
        import numpy as np

        if options["from_matrix"]:
            matrix = RewardMatrix.load(options["from_matrix"])
            X, y, vocab, source_means = xgb_model.build_training_data_from_matrix(matrix)
            if len(y) < options["min_rows"]:
                raise CommandError(
                    f"only {len(y)} training pairs in the matrix, need {options['min_rows']} (--min-rows)"
                )
            model = xgb_model.train(X, y)
            metrics = {"train_rmse": float(np.sqrt(np.mean((model.predict(X) - y) ** 2)))}
            xgb_model.save_artifact(
                model,
                vocab,
                options["out"],
                reward_kind=options["reward_kind"],
                n_rows=len(y),
                metrics=metrics,
                source_reward_means=source_means,
            )
            if not options["no_profiles"]:
                fetch = Path(options["profiles_from"] or f"{options['from_matrix']}.fetch.jsonl")
                if not fetch.exists():
                    raise CommandError(f"fetch checkpoint {fetch} not found; pass --profiles-from or --no-profiles")
                profiles = profiles_from_fetch(fetch, settings.SUPER_SEARCH_PROJECTION_DIM)
                xgb_model.save_profiles(profiles, options["out"])
                self.stdout.write(f"wrote profiles.npz for {len(profiles)} sources")
        else:
            metrics = xgb_model.train_and_save_from_impressions(
                window_days=options["days"],
                min_rows=options["min_rows"],
                out_dir=options["out"],
            )
            if metrics is None:
                raise CommandError("not enough impression pairs to train (see --min-rows)")
        self.stdout.write(self.style.SUCCESS(f"trained xgb source model -> {options['out']} ({metrics})"))
