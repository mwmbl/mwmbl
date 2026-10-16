#!/usr/bin/env python3
"""Human-agreement eval: judge accuracy on held-out curation preference pairs.

For each judge, scores both sides of every pair in
devdata/judge_train/pairs_eval.jsonl.gz and reports pairwise accuracy
P(score(pos) > score(neg)) — overall, by rule (add/move/approve), and for the
top contributor vs everyone else (a fine-tuned judge that only wins on the top
user has overfitted one curator's taste; note the per-user train cap already
limits their share).

Judges: the zero-shot judges from judge_bakeoff.py (needs fastembed), plus
fine-tuned ONNX judges via --model-dir (needs onnxruntime+tokenizers), plus
`ft_<run>_torch` read from the artifact's pairs_eval_scores.npz. Pos/neg score
arrays are cached in devdata/judge_bakeoff/pairscores_{judge}.npz.

Run:  uv run --with fastembed python scripts/judge_pairs_eval.py \
          [--judges a,b] [--model-dir devdata/judge_train/models/<run>]
"""

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import judge_bakeoff
from judge_bakeoff import ALL_JUDGES, CACHE_DIR, SCORERS, register_finetuned

PAIRS_EVAL = Path("devdata/judge_train/pairs_eval.jsonl.gz")


def load_pairs() -> list[dict]:
    with gzip.open(PAIRS_EVAL, "rt") as f:
        return [json.loads(line) for line in f]


def pair_scores(name: str, pairs: list[dict], model_dirs: list[Path]) -> tuple[np.ndarray, np.ndarray]:
    cache = CACHE_DIR / f"pairscores_{name}.npz"
    if cache.exists():
        data = np.load(cache)
        if len(data["pos"]) == len(pairs):
            return data["pos"], data["neg"]
    if name.endswith("_torch"):
        for model_dir in model_dirs:
            if f"ft_{model_dir.name}_torch" == name:
                data = np.load(model_dir / "pairs_eval_scores.npz")
                pos, neg = data["pos"], data["neg"]
                break
        else:
            raise ValueError(f"no artifact provides {name}")
    else:
        print(f"scoring {name} ({len(pairs)} pairs)...", flush=True)
        docs = judge_bakeoff.DOC_CHARS
        pos = SCORERS[name]([{"query": p["query"], "doc_text": p["pos"][:docs]} for p in pairs])
        neg = SCORERS[name]([{"query": p["query"], "doc_text": p["neg"][:docs]} for p in pairs])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(cache, pos=pos, neg=neg)
    return pos, neg


def accuracy(pos: np.ndarray, neg: np.ndarray, mask=None) -> str:
    if mask is not None:
        pos, neg = pos[mask], neg[mask]
    if not len(pos):
        return "n/a"
    wins = float(np.mean(pos > neg))
    return f"{wins:.3f} (n={len(pos)})"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--judges", default=None)
    parser.add_argument("--model-dir", type=Path, action="append", default=[])
    args = parser.parse_args()

    finetuned = [name for model_dir in args.model_dir for name in register_finetuned(model_dir)]
    judges = args.judges.split(",") if args.judges else list(ALL_JUDGES) + finetuned

    pairs = load_pairs()
    rules = sorted({p["rule"] for p in pairs})
    top_user = Counter(p["user"] for p in pairs).most_common(1)[0][0]
    print(f"{len(pairs)} held-out pairs, rules {rules}, top user {top_user}\n")

    results = []
    for name in judges:
        pos, neg = pair_scores(name.strip(), pairs, args.model_dir)
        row = {"judge": name, "accuracy": accuracy(pos, neg)}
        for rule in rules:
            mask = np.array([p["rule"] == rule for p in pairs])
            row[rule] = accuracy(pos, neg, mask)
        top_mask = np.array([p["user"] == top_user for p in pairs])
        row[f"{top_user}"] = accuracy(pos, neg, top_mask)
        row["others"] = accuracy(pos, neg, ~top_mask)
        results.append(row)
        print(json.dumps(row))

    (CACHE_DIR / "results_pairs_eval.json").write_text(json.dumps(results, indent=2))
    print("\n=== pairs-eval accuracy ===")
    keys = list(results[0].keys())
    print(" | ".join(f"{k:>24s}" if i == 0 else k for i, k in enumerate(keys)))
    for row in results:
        print(" | ".join(f"{str(row[k]):>24s}" if i == 0 else str(row[k]) for i, k in enumerate(keys)))


if __name__ == "__main__":
    main()
