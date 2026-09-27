"""Step 4 of the structural-fixes study: the tables in docs/index-structural-fixes.md.

Calibrates the grades of unjudged results against Haiku, then scores each design's lists
with every unjudged result at a fixed gain, with 95% bootstrap CIs over queries.

    uv run python scripts/combined_search_haiku/structural_report.py
"""

import json

import numpy as np
from structural_common import GRADES_PATH, NUM_RESULTS, load_grades, load_rows, load_work, query_ndcg, staan_first

UNJUDGED_GAINS = (1.0, 0.8, 1.2, 2.0)
BOOTSTRAP_SAMPLES = 4000


def bootstrap(values: np.ndarray, rng) -> tuple[float, float]:
    means = [values[rng.integers(0, len(values), len(values))].mean() for _ in range(BOOTSTRAP_SAMPLES)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def calibrate() -> None:
    records = [json.loads(line) for line in GRADES_PATH.read_text().splitlines()]
    calibration = [r for r in records if r["kind"] == "calibration"]
    ours = np.array([r["grade"] for r in calibration])
    haiku = np.array([r["haiku"] for r in calibration])
    print(
        f"Calibration ({len(calibration)} judged results): {np.mean(ours == haiku):.0%} identical, "
        f"{np.mean(abs(ours - haiku) <= 1):.0%} within one grade"
    )
    # Our grades mapped to the mean Haiku gain of the calibration results given each one.
    haiku_gain = {grade: float((2.0 ** haiku[ours == grade] - 1).mean()) for grade in range(4)}
    rng = np.random.default_rng(0)
    for kind in ("newcomer", "baseline"):
        gains = np.array([haiku_gain[r["grade"]] for r in records if r["kind"] == kind])
        low, high = bootstrap(gains, rng)
        print(f"Unjudged {kind} results ({len(gains)}): calibrated gain {gains.mean():.2f} [{low:.2f}, {high:.2f}]")


def scores(lists: dict[str, list[str]], rows, grades, unjudged_gain: float) -> np.ndarray:
    """Per query: index-only NDCG@10 and Staan-first NDCG@10."""
    per_query = []
    for row in rows:
        query, index_only = row["query"], lists[row["query"]]
        production = staan_first(row["lists"]["staan"][:NUM_RESULTS], index_only)
        per_query.append(
            [query_ndcg(index_only, grades[query], unjudged_gain), query_ndcg(production, grades[query], unjudged_gain)]
        )
    return np.array(per_query)


def main():
    calibrate()
    lists, rows, grades = load_work("lists.json"), load_rows(), load_grades()
    for unjudged_gain in UNJUDGED_GAINS:
        rng = np.random.default_rng(0)
        baseline = scores(lists["baseline"], rows, grades, unjudged_gain)
        print(
            f"\n## Unjudged gain {unjudged_gain}: baseline index-only {baseline[:, 0].mean():.3f}, "
            f"Staan-first {baseline[:, 1].mean():.3f}\n"
        )
        print(
            "| Design | Recovered | Targets only: index-only | With deep documents: index-only | With deep documents: Staan-first |"
        )
        print("|---|---|---|---|---|")
        for name, recovered in lists["recovered"].items():
            cells = []
            for mode in ("targets", "deep"):
                draws = [scores(draw, rows, grades, unjudged_gain) for draw in lists[mode][name]]
                differences = np.mean(draws, axis=0) - baseline
                columns = (0,) if mode == "targets" else (0, 1)
                for column in columns:
                    low, high = bootstrap(differences[:, column], rng)
                    cells.append(f"{differences[:, column].mean():+.3f} [{low:+.3f}, {high:+.3f}]")
            print(f"| {name} | {recovered:.0%} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
