#!/usr/bin/env python3
"""Compare an LLM's approve/reject judgements against the trained suggester, on identical rows.

The question this answers is not "how accurate is the LLM" - accuracy on a slice whose base
rate moves is not a number you can compare to anything. It is "does the LLM rank the domains a
moderator will reject above the ones they will approve, better than the model we ship?", scored
with exactly the machinery in mwmbl.moderation.train so the two numbers are on one scale.

Three steps, because the middle one runs outside this process:

    # 1. write the held-out cold-start rows, domain names only, with no labels in the file
    uv run python scripts/moderation_llm_bakeoff.py export --out devdata/moderation_bakeoff

    # 2. have an LLM judge devdata/moderation_bakeoff/domains.txt into a JSONL of
    #    {"domain": ..., "reject_probability": 0-100} and save it beside that file

    # 3. score every judgement file against the model, paired, on the same rows
    uv run python scripts/moderation_llm_bakeoff.py score --dir devdata/moderation_bakeoff

The export deliberately carries no labels and no submitter, so the file handed to a judge -
which may be a third party - cannot leak the answer or say who submitted what.
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

import numpy as np                                                # noqa: E402
from sklearn.metrics import average_precision_score               # noqa: E402

from mwmbl.moderation.train import (                              # noqa: E402
    BOOTSTRAP_SAMPLES, normalise, split_by_time, train)
from mwmbl.moderation.training_data import seed_rows              # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from moderation_eval import DEFAULT_EXPORT, rows_from_export      # noqa: E402

DOMAINS_FILE = "domains.txt"
TRUTH_FILE = "truth.jsonl"
JUDGEMENT_SUFFIX = ".judgements.jsonl"


def cold_start_test_rows(export: Path):
    """The held-out rows from submitters with no track record, and the model's scores for them.

    Rebuilt here rather than passed in, so the rows an LLM is asked about are the same rows the
    gate reads - a bake-off on a different slice would answer a different question.
    """
    rows = rows_from_export(export) + seed_rows()
    model, _ = train(rows, "bakeoff")
    train_rows, test_rows = split_by_time(rows)

    known = {row.submitter for row in train_rows if row.submitter}
    cold = [row for row in test_rows if row.submitter not in known]
    scores = [prediction[0] for prediction in
              model.predict([row.to_example() for row in cold])]
    return cold, np.array(scores)


def export(options) -> int:
    rows, scores = cold_start_test_rows(options.export)
    options.out.mkdir(parents=True, exist_ok=True)

    (options.out / DOMAINS_FILE).write_text(
        "".join(f"{row.domain}\n" for row in rows))
    # The labels and the model's own scores stay on this side, keyed by domain, so scoring can
    # join them back without the judge ever having seen them.
    with (options.out / TRUTH_FILE).open("w") as truth:
        for row, score in zip(rows, scores):
            truth.write(json.dumps({"domain": row.domain, "rejected": row.rejected,
                                    "reason": row.reason, "model_score": float(score)}) + "\n")

    print(f"{len(rows)} cold-start held-out domains -> {options.out / DOMAINS_FILE}")
    print(f"{sum(row.rejected for row in rows)} of them were rejected "
          f"(base rate {sum(row.rejected for row in rows) / len(rows):.3f})")
    return 0


def read_judgements(path: Path) -> dict:
    """A judge's file as {domain: reject probability in [0, 1]}.

    Tolerant about the scale because judges are asked for 0-100 and some answer 0-1, but the
    scale is decided **once for the whole file**, not per value. Deciding per value reads a
    judge's single most confident approval - a 1 out of 100 - as a certainty of 1.0, turning it
    into the most confident *rejection* in the file. Both judges in the first bake-off emitted
    exactly one such row, and it cost each of them real score.

    Not tolerant about anything else: a malformed line is a judgement we cannot score, and
    silently dropping it would quietly change the slice.
    """
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        record = json.loads(line)
        records.append((record["domain"], float(record["reject_probability"])))

    percentages = any(score > 1 for _, score in records)
    return {domain: score / 100 if percentages else score for domain, score in records}


def paired_bootstrap(truth: np.ndarray, left: np.ndarray, right: np.ndarray) -> dict:
    """Normalised AP for two rankers over the same rows, and an interval on the difference.

    Resampling both on the same indices cancels the variance that comes from which rows landed
    in the test set, which is most of it on a slice this size.
    """
    generator = np.random.default_rng(0)
    differences = []
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = generator.choice(len(truth), len(truth), replace=True)
        sample = truth[indices]
        if sample.sum() < 5 or (1 - sample).sum() < 5:
            continue
        base = sample.mean()
        differences.append(normalise(average_precision_score(sample, left[indices]), base)
                           - normalise(average_precision_score(sample, right[indices]), base))

    base_rate = float(truth.mean())
    return {
        "left": round(normalise(average_precision_score(truth, left), base_rate), 4),
        "right": round(normalise(average_precision_score(truth, right), base_rate), 4),
        "difference": round(
            normalise(average_precision_score(truth, left), base_rate)
            - normalise(average_precision_score(truth, right), base_rate), 4),
        "difference_ci": [round(float(np.percentile(differences, 2.5)), 4),
                          round(float(np.percentile(differences, 97.5)), 4)],
        "left_wins_fraction": round(float(np.mean(np.array(differences) > 0)), 4),
    }


def score(options) -> int:
    truth_rows = [json.loads(line) for line in
                  (options.dir / TRUTH_FILE).read_text().splitlines() if line.strip()]
    # One entry per distinct domain, not per submission. A domain submitted nine times is one
    # thing a moderator decides, and counting it nine times would weight the score by how
    # popular a submission was rather than by how hard it was to judge. Both rankers are
    # deduplicated the same way, so the comparison is unaffected either way.
    by_domain = {row["domain"]: row for row in truth_rows}
    print(f"{len(truth_rows)} held-out submissions, {len(by_domain)} distinct domains")

    files = sorted(options.dir.glob(f"*{JUDGEMENT_SUFFIX}"))
    if not files:
        print(f"No *{JUDGEMENT_SUFFIX} files in {options.dir}", file=sys.stderr)
        return 1

    for path in files:
        judgements = read_judgements(path)
        # Only rows this judge actually answered, so a judge that skipped the hard ones is
        # visible as coverage rather than hidden in a score computed over a smaller slice.
        answered = [domain for domain in by_domain if domain in judgements]
        truth = np.array([by_domain[domain]["rejected"] for domain in answered], dtype=int)
        llm = np.array([judgements[domain] for domain in answered])
        model = np.array([by_domain[domain]["model_score"] for domain in answered])

        result = paired_bootstrap(truth, llm, model)
        print(f"\n{path.name}")
        print(f"  answered {len(answered)}/{len(by_domain)} domains, "
              f"{int(truth.sum())} rejected (base rate {truth.mean():.3f})")
        print(f"  normalised AP: LLM {result['left']:.4f}  model {result['right']:.4f}  "
              f"difference {result['difference']:+.4f} "
              f"[{result['difference_ci'][0]:+.4f}, {result['difference_ci'][1]:+.4f}]")
        print(f"  the LLM ranks better in {result['left_wins_fraction']:.0%} of resamples")
        print(f"  raw PR-AUC: LLM "
              f"{average_precision_score(truth, llm):.4f}  model "
              f"{average_precision_score(truth, model):.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    exporter = subparsers.add_parser("export")
    exporter.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    exporter.add_argument("--out", type=Path, default=Path("devdata/moderation_bakeoff"))
    exporter.set_defaults(function=export)

    scorer = subparsers.add_parser("score")
    scorer.add_argument("--dir", type=Path, default=Path("devdata/moderation_bakeoff"))
    scorer.set_defaults(function=score)

    options = parser.parse_args()
    return options.function(options)


if __name__ == "__main__":
    sys.exit(main())
