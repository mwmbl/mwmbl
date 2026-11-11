"""Score Jev's image-need gate and image ranking against the Sonnet reference labels, on the train queries.

Gate: AUC of each gate signal against reference need >= 2, and a cross-validated threshold's precision and
recall. Ranking: NDCG@k of Jev relevance, Commons rank and a cross-validated mix of the two, over each sampled
query's labelled candidates, then how text labels agree with the vision spot-check, then what each choice of
k and minimum Jev relevance would show. Headline figures get 95% intervals from resampling queries with
replacement. Prints markdown tables.

    uv run python -m mwmbl.rankeval.seed_images.gate_analysis --arm hardened

`--arm` picks whose answers are scored: `first`, the questions as first asked (the default); `hardened`, the
hardened questions in production's one request (run_hardened); `top1`, hardened relevance given one web result;
`captions`, hardened relevance with Commons captions.
The first answers can only be re-scored, not re-asked: the questions that gave them are now the hardened ones.
"""

import json
from argparse import ArgumentParser
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import KFold

from mwmbl.imagesearch import prompt
from mwmbl.rankeval.seed_images.gate_data import GATE_DIR, NSFW_THRESHOLD, RELEVANCE_PATH, jev_safe_candidates
from mwmbl.rankeval.seed_images.queries import load_queries, read_jsonl
from mwmbl.rankeval.seed_images.run_jev import NEED_PATH

POSITIVE_LEVEL = 2
FOLDS = 5
SEED = 610
NDCG_KS = (1, 3, 5)
TUNE_K = 3
MIX_WEIGHTS = (0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 100.0)
SHOWN_KS = (3, 4, 6)
MIN_RELEVANCES = (0.0, 1.0, 1.5, 2.0, 2.5)
INTENTS_WANTING_IMAGES = {"entity", "local"}
# Chosen from the tables this prints; see mwmbl/rankeval/seed-search-images-eval.md.
RECOMMENDED_K = 4
RECOMMENDED_PATH = GATE_DIR / "recommended.json"
# The hardened questions' answers (run_hardened).
HARDENED_PATH = GATE_DIR / "hardened_image.jsonl"
TOP1_PATH = GATE_DIR / "hardened_top1.jsonl"
# The same request with Commons captions, on the queries with relevance labels (captions.py).
CAPTIONS_PATH = GATE_DIR / "captions_image.jsonl"
# arm: (need answers, their need field, relevance answers, recommended minimum relevance, where to record it)
ARMS = {
    "first": (NEED_PATH, "score", RELEVANCE_PATH, 1.5, RECOMMENDED_PATH),
    "hardened": (HARDENED_PATH, "need", HARDENED_PATH, prompt.MIN_RELEVANCE, GATE_DIR / "recommended_hardened.json"),
    "top1": (HARDENED_PATH, "need", TOP1_PATH, prompt.MIN_RELEVANCE, GATE_DIR / "recommended_top1.json"),
    # Need doesn't read the images, so captions leave the hardened gate as it was.
    "captions": (HARDENED_PATH, "need", CAPTIONS_PATH, prompt.MIN_RELEVANCE, GATE_DIR / "recommended_captions.json"),
}
BOOTSTRAP_SAMPLES = 2000


def bootstrap_interval(statistic: Callable[[np.ndarray], float], units: int) -> tuple[float, float]:
    """The 95% percentile interval of `statistic` over resamples, with replacement, of `units` query indices."""
    rng = np.random.default_rng(SEED)
    values = [statistic(rng.integers(0, units, units)) for _ in range(BOOTSTRAP_SAMPLES)]
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def interval_text(statistic: Callable[[np.ndarray], float], units: int) -> str:
    low, high = bootstrap_interval(statistic, units)
    return f"[{low:.3f}, {high:.3f}]"


def gate_stats(gated_in: np.ndarray, positives: np.ndarray) -> dict:
    true_positives = np.sum(gated_in & positives)
    precision = true_positives / gated_in.sum() if gated_in.any() else float("nan")
    return {"share": gated_in.mean(), "precision": precision, "recall": true_positives / positives.sum()}


def best_threshold(scores: np.ndarray, positives: np.ndarray) -> float:
    """The threshold, gating in scores at or above it, with the highest F1."""

    def f1(threshold: float) -> float:
        stats = gate_stats(scores >= threshold, positives)
        return 2 * stats["precision"] * stats["recall"] / (stats["precision"] + stats["recall"])

    return max(np.unique(scores), key=f1)


def cross_validated_gate(scores: np.ndarray, positives: np.ndarray) -> np.ndarray:
    gated_in = np.zeros(len(scores), dtype=bool)
    for train, test in KFold(FOLDS, shuffle=True, random_state=SEED).split(scores):
        gated_in[test] = scores[test] >= best_threshold(scores[train], positives[train])
    return gated_in


def ndcg(labels_in_order: list[int], k: int) -> float | None:
    """NDCG@k with gain 2^label - 1, the ideal order being the same labels sorted; None if every label is 0."""
    discounts = 1 / np.log2(np.arange(2, k + 2))
    gains = 2 ** np.array(labels_in_order, dtype=float) - 1
    ideal = np.sort(gains)[::-1][:k] @ discounts[: min(k, len(gains))]
    if ideal == 0:
        return None
    return float(gains[:k] @ discounts[: min(k, len(gains))] / ideal)


def mix_order(images: list[dict], weight: float) -> list[dict]:
    """Highest Jev relevance first, less `weight` per place down Commons' order; ties go to Commons' order."""
    return sorted(
        images, key=lambda image: (-(image["jev"] - weight * (image["commons_rank"] - 1)), image["commons_rank"])
    )


def mean_ndcg(pools: list[list[dict]], weight: float, k: int) -> float:
    values = [ndcg([image["relevance"] for image in mix_order(pool, weight)], k) for pool in pools]
    return float(np.mean([value for value in values if value is not None]))


def cross_validated_mix(pools: list[list[dict]]) -> tuple[dict[int, float], list[float]]:
    """Out-of-fold NDCG@k of the mix whose weight each fold tunes on the others, and the weights chosen."""
    held_out = {k: [] for k in NDCG_KS}
    chosen = []
    for train, test in KFold(FOLDS, shuffle=True, random_state=SEED).split(pools):
        weight = max(MIX_WEIGHTS, key=lambda w: mean_ndcg([pools[i] for i in train], w, TUNE_K))
        chosen.append(weight)
        for k in NDCG_KS:
            held_out[k].append(mean_ndcg([pools[i] for i in test], weight, k))
    return {k: float(np.mean(values)) for k, values in held_out.items()}, chosen


def agreement(scores: list[float], labels: list[int]) -> dict:
    """Rank correlation of scores with labels, and how often the scores, rounded, match them exactly or to 1."""
    differences = np.abs(np.rint(scores) - np.array(labels))
    rho = spearmanr(scores, labels).statistic
    return {"spearman": rho, "exact": np.mean(differences == 0), "within_one": np.mean(differences <= 1)}


def shown(pools: list[list[dict]], k: int, min_relevance: float) -> dict:
    """What showing each query's top k by Jev relevance, those at or above min_relevance, would put on pages."""
    shown_labels = [
        [image["relevance"] for image in mix_order(pool, 0.0)[:k] if image["jev"] >= min_relevance] for pool in pools
    ]
    flat = [label for labels in shown_labels for label in labels]
    return {
        "per_query": np.mean([len(labels) for labels in shown_labels]),
        "any_shown": np.mean([bool(labels) for labels in shown_labels]),
        "good": np.mean([label >= POSITIVE_LEVEL for label in flat]),
        "wrong": np.mean([label == 0 for label in flat]),
    }


def analyse_gate(need_path: Path, need_field: str) -> tuple[float, dict[str, set[str]]]:
    reference = {row["query"]: row["need"] for row in read_jsonl(GATE_DIR / "labels_need.jsonl")}
    jev_need = {row["query"]: row[need_field] for row in read_jsonl(need_path)}
    queries = load_queries()
    safe = jev_safe_candidates()
    ordered = sorted(reference)
    positives = np.array([reference[query] >= POSITIVE_LEVEL for query in ordered])
    signals = {
        "Jev need": np.array([jev_need[query] for query in ordered]),
        "Jev-safe candidate count": np.array([len(safe.get(query, [])) for query in ordered], dtype=float),
        "Pass-1 intent entity/local": np.array(
            [queries[query]["intent"] in INTENTS_WANTING_IMAGES for query in ordered]
        ),
    }
    print(f"Reference need >= {POSITIVE_LEVEL}: {positives.sum()}/{len(ordered)} ({positives.mean():.1%})\n")
    print("| Gate | AUC | Gated in | Precision | Recall |\n|---|---|---|---|---|")
    fixed = {"Always": np.ones(len(ordered), dtype=bool), "Never": np.zeros(len(ordered), dtype=bool)}
    for name, gated_in in fixed.items():
        stats = gate_stats(gated_in, positives)
        print(f"| {name} | 0.500 | {stats['share']:.1%} | {stats['precision']:.3f} | {stats['recall']:.3f} |")
    for name, scores in signals.items():
        auc = roc_auc_score(positives, scores)
        gated_in = scores.astype(bool) if scores.dtype == bool else cross_validated_gate(scores, positives)
        stats = gate_stats(gated_in, positives)
        print(f"| {name} | {auc:.3f} | {stats['share']:.1%} | {stats['precision']:.3f} | {stats['recall']:.3f} |")
    jev_scores, intent_scores = signals["Jev need"], signals["Pass-1 intent entity/local"].astype(float)

    def auc_of(scores: np.ndarray) -> Callable[[np.ndarray], float]:
        return lambda sample: roc_auc_score(positives[sample], scores[sample])

    def auc_difference(sample: np.ndarray) -> float:
        return auc_of(jev_scores)(sample) - auc_of(intent_scores)(sample)

    units = len(ordered)
    print(f"\nAUC 95% intervals: Jev need {interval_text(auc_of(jev_scores), units)}, intent ", end="")
    print(f"{interval_text(auc_of(intent_scores), units)}, Jev minus intent {interval_text(auc_difference, units)}")
    threshold = best_threshold(signals["Jev need"], positives)
    print(f"\nJev need threshold on all train queries: {threshold:.2f}\n")
    query_sets = {
        f"reference need >= {POSITIVE_LEVEL}": {query for query in ordered if reference[query] >= POSITIVE_LEVEL},
        f"Jev need >= {threshold:.2f}": {query for query in ordered if jev_need[query] >= threshold},
    }
    return float(threshold), query_sets


def ndcg_intervals(pools: list[list[dict]]) -> None:
    """Intervals for Jev relevance's and Commons rank's mean NDCG@k, and their paired difference."""
    scorable = [pool for pool in pools if ndcg([image["relevance"] for image in pool], 1) is not None]
    for k in NDCG_KS:
        per_query = {
            name: np.array([ndcg([image["relevance"] for image in mix_order(pool, weight)], k) for pool in scorable])
            for name, weight in (("Jev", 0.0), ("Commons", MIX_WEIGHTS[-1]))
        }
        difference = per_query["Jev"] - per_query["Commons"]
        texts = [
            interval_text(lambda sample, v=values: v[sample].mean(), len(scorable)) for values in per_query.values()
        ]
        print(f"NDCG@{k} 95% intervals: Jev {texts[0]}, Commons {texts[1]}, ", end="")
        print(f"difference {interval_text(lambda sample: difference[sample].mean(), len(scorable))}")
    print()


def analyse_ranking(query_sets: dict[str, set[str]], relevance_path: Path, min_relevance: float) -> None:
    jev = {
        (row["query"], file): score
        for row in read_jsonl(relevance_path)
        for file, score in zip(row["file_ids"], row["scores"])
    }
    by_query: dict[str, list[dict]] = {}
    for row in read_jsonl(GATE_DIR / "labels_relevance.jsonl"):
        by_query.setdefault(row["query"], []).append(row | {"jev": jev[(row["query"], row["file_id"])]})
    pools = list(by_query.values())
    print(f"{len(pools)} queries, {sum(map(len, pools))} labelled images\n")
    print("| Order | " + " | ".join(f"NDCG@{k}" for k in NDCG_KS) + " |\n|---|" + "---|" * len(NDCG_KS))
    for name, weight in (("Jev relevance", 0.0), ("Commons rank", MIX_WEIGHTS[-1])):
        print(f"| {name} | " + " | ".join(f"{mean_ndcg(pools, weight, k):.3f}" for k in NDCG_KS) + " |")
    ndcg_intervals(pools)
    held_out, weights = cross_validated_mix(pools)
    print("| Mix (5-fold CV) | " + " | ".join(f"{held_out[k]:.3f}" for k in NDCG_KS) + f" |\n\nWeights: {weights}\n")

    vision = list(read_jsonl(GATE_DIR / "labels_vision.jsonl"))
    text_label = {(row["query"], row["file_id"]): row["relevance"] for pool in pools for row in pool}
    vision_labels = [row["relevance"] for row in vision]
    print("| Against vision | Spearman | Exact | ±1 |\n|---|---|---|---|")
    for name, source in (("Text label", text_label), ("Jev relevance", jev)):
        values = [source[(row["query"], row["file_id"])] for row in vision]
        stats = agreement(values, vision_labels)
        print(f"| {name} | {stats['spearman']:.3f} | {stats['exact']:.1%} | {stats['within_one']:.1%} |")

    for name, queries in query_sets.items():
        gated_pools = [pool for pool in pools if pool[0]["query"] in queries]
        print(f"\nOn the {len(gated_pools)} sampled queries with {name}:\n")
        print("| k | Min Jev relevance | Images per query | Queries with any | Good (>=2) | Wrong (0) |")
        print("|---|---|---|---|---|---|")
        for k in SHOWN_KS:
            for minimum in MIN_RELEVANCES:
                stats = shown(gated_pools, k, minimum)
                print(
                    f"| {k} | {minimum} | {stats['per_query']:.2f} | {stats['any_shown']:.1%} | "
                    f"{stats['good']:.1%} | {stats['wrong']:.1%} |"
                )
        for share in ("good", "wrong"):

            def resampled_share(sample: np.ndarray, share: str = share) -> float:
                return shown([gated_pools[i] for i in sample], RECOMMENDED_K, min_relevance)[share]

            interval = interval_text(resampled_share, len(gated_pools))
            print(f"k = {RECOMMENDED_K}, min {min_relevance}: {share} share 95% interval {interval}")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arm", choices=ARMS, default="first")
    need_path, need_field, relevance_path, min_relevance, recommended_path = ARMS[parser.parse_args().arm]
    threshold, query_sets = analyse_gate(need_path, need_field)
    analyse_ranking(query_sets, relevance_path, min_relevance)
    recommended = {
        "jev_need_threshold": round(threshold, 2),
        "num_images": RECOMMENDED_K,
        "min_jev_relevance": min_relevance,
        "nsfw_threshold": NSFW_THRESHOLD,
        "order": "Jev image relevance, highest first; ties by Commons rank",
    }
    recommended_path.write_text(json.dumps(recommended, indent=2) + "\n")
    print(f"\n{recommended} -> {recommended_path}")


if __name__ == "__main__":
    main()
