#!/usr/bin/env python3
"""Zero-shot judge bake-off against the LLM-graded relevance labels.

Scores every (query, doc) row of learning-to-rank-llm.csv.gz with each candidate
cheap judge and reports how well each agrees with the LLM labels. The goal is to
pick (or set the fine-tuning baseline for) a local model that can grade real
user queries at serve time as the contextual bandit's reward signal.

Judges (all CPU, via fastembed/ONNX except the trivial baseline):
- term_overlap   fraction of query tokens appearing in title+extract
- nomic_cosine   nomic-embed-text-v1.5 bi-encoder cosine (query/doc prefixes)
- minilm_ce      Xenova/ms-marco-MiniLM-L-6-v2 cross-encoder
- jina_turbo_ce  jinaai/jina-reranker-v1-turbo-en cross-encoder

Metrics vs the LLM labels:
- pointwise: global + mean per-query Spearman vs `overall` (0-10) and
  `relevance` (0-3); ROC-AUC for relevance >= 2
- ranking: mean NDCG@10 with gain = `overall` (random-order baseline shown)
- source-level (what the bandit actually consumes): among Super Search rows,
  per-(query, ss_source) mean judge score vs mean LLM overall — within-query
  Spearman across sources, and best-source agreement@1

Scores are cached per judge in devdata/judge_bakeoff/ so reruns are instant.

Fine-tuned judges (see scripts/modal_judge_train.py): pass --model-dir
devdata/judge_train/models/<run> to register `ft_<run>` (ONNX fp32),
`ft_<run>_int8` (quantized) and `ft_<run>_torch` (score cache exported by the
training job). ONNX judges need onnxruntime+tokenizers instead of fastembed.

Held-out mode: --eval-manifest devdata/judge_train/eval_manifest.json scores
the full dataset (caches stay full-length) but computes metrics only on the
manifest's held-out rows, writing results_heldout.json — fine-tuned judges must
only ever be reported in this mode.

Run:  uv run --with fastembed python scripts/judge_bakeoff.py [--judges a,b,...]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

DATASET = Path("devdata/rankeval-2026-04/learning-to-rank-llm.csv.gz")
CACHE_DIR = Path("devdata/judge_bakeoff")
DOC_CHARS = 1000  # tokenizers truncate anyway; keep prompt assembly cheap

ALL_JUDGES = ("term_overlap", "nomic_cosine", "minilm_ce", "jina_turbo_ce")


def load_dataset() -> list[dict]:
    with gzip.open(DATASET, "rt") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["overall"] = float(row["overall"])
        row["relevance"] = float(row["relevance"])
        row["doc_text"] = f"{row['title'] or ''}. {row['extract'] or ''}".strip()[:DOC_CHARS]
    return rows


# --- judges -----------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def score_term_overlap(rows: list[dict]) -> np.ndarray:
    scores = np.zeros(len(rows))
    for i, row in enumerate(rows):
        query_tokens = set(tokenize(row["query"]))
        if not query_tokens:
            continue
        doc_tokens = set(tokenize(row["doc_text"]))
        scores[i] = len(query_tokens & doc_tokens) / len(query_tokens)
    return scores


def score_nomic_cosine(rows: list[dict]) -> np.ndarray:
    from fastembed import TextEmbedding

    model = TextEmbedding("nomic-ai/nomic-embed-text-v1.5")
    queries = sorted({row["query"] for row in rows})
    query_vectors = dict(zip(queries, model.embed([f"search_query: {q}" for q in queries], batch_size=64)))
    doc_vectors = model.embed([f"search_document: {row['doc_text']}" for row in rows], batch_size=64)

    scores = np.zeros(len(rows))
    for i, (row, doc_vector) in enumerate(zip(rows, doc_vectors)):
        query_vector = query_vectors[row["query"]]
        scores[i] = float(
            np.dot(query_vector, doc_vector) / (np.linalg.norm(query_vector) * np.linalg.norm(doc_vector) + 1e-9)
        )
        if (i + 1) % 5000 == 0:
            print(f"  nomic: {i + 1}/{len(rows)}", flush=True)
    return scores


def _cross_encoder(rows: list[dict], model_name: str, label: str) -> np.ndarray:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name)
    scores = np.zeros(len(rows))
    by_query = defaultdict(list)
    for i, row in enumerate(rows):
        by_query[row["query"]].append(i)
    done = 0
    for query, indexes in by_query.items():
        for index, score in zip(indexes, model.rerank(query, [rows[i]["doc_text"] for i in indexes], batch_size=64)):
            scores[index] = float(score)
        done += len(indexes)
        if done // 5000 != (done - len(indexes)) // 5000:
            print(f"  {label}: {done}/{len(rows)}", flush=True)
    return scores


def score_minilm_ce(rows: list[dict]) -> np.ndarray:
    return _cross_encoder(rows, "Xenova/ms-marco-MiniLM-L-6-v2", "minilm_ce")


def score_jina_turbo_ce(rows: list[dict]) -> np.ndarray:
    return _cross_encoder(rows, "jinaai/jina-reranker-v1-turbo-en", "jina_turbo_ce")


def onnx_cross_encoder(
    rows: list[dict], onnx_dir: Path, model_file: str, label: str, max_length: int = 256, batch_size: int = 64
) -> np.ndarray:
    """Score (query, doc_text) rows with a local exported cross-encoder."""
    import onnxruntime
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(onnx_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=max_length)
    tokenizer.enable_padding()
    session = onnxruntime.InferenceSession(str(onnx_dir / model_file))
    input_names = {i.name for i in session.get_inputs()}

    scores = np.zeros(len(rows))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        encodings = tokenizer.encode_batch([(row["query"], row["doc_text"]) for row in batch])
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        logits = session.run(None, feed)[0][:, 0]
        scores[start : start + len(batch)] = 1 / (1 + np.exp(-logits.astype(np.float64)))
        if (start + batch_size) % 4992 < batch_size:
            print(f"  {label}: {start + len(batch)}/{len(rows)}", flush=True)
    return scores


SCORERS = {
    "term_overlap": score_term_overlap,
    "nomic_cosine": score_nomic_cosine,
    "minilm_ce": score_minilm_ce,
    "jina_turbo_ce": score_jina_turbo_ce,
}


def register_finetuned(model_dir: Path) -> list[str]:
    """Register `ft_<run>` judges for a modal_judge_train.py artifact dir."""
    run = model_dir.name
    names = []
    onnx_dir = model_dir / "onnx"
    for suffix, model_file in (("", "model.onnx"), ("_int8", "model.int8.onnx")):
        if (onnx_dir / model_file).exists():
            name = f"ft_{run}{suffix}"
            SCORERS[name] = lambda rows, f=model_file, n=f"ft_{run}{suffix}": onnx_cross_encoder(rows, onnx_dir, f, n)
            names.append(name)
    torch_scores = model_dir / "llm_scores.npy"
    if torch_scores.exists():
        name = f"ft_{run}_torch"
        cache = CACHE_DIR / f"scores_{name}.npy"
        if not cache.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(torch_scores.read_bytes())
        names.append(name)
    return names


def judge_scores(name: str, rows: list[dict]) -> np.ndarray:
    cache = CACHE_DIR / f"scores_{name}.npy"
    if cache.exists():
        scores = np.load(cache)
        if len(scores) == len(rows):
            return scores
    print(f"scoring {name} ({len(rows)} rows)...", flush=True)
    scores = SCORERS[name](rows)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(cache, scores)
    return scores


# --- metrics ----------------------------------------------------------------


def rankdata(values) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    order = np.argsort(values)
    ranks = np.empty(len(values))
    ranks[order] = np.arange(len(values), dtype=float)
    # average ties
    unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    sums = np.zeros(len(unique))
    np.add.at(sums, inverse, ranks)
    return sums[inverse] / counts[inverse]


def spearman(a, b) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    ra, rb = rankdata(a), rankdata(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return float("nan")
    ranks = rankdata(scores)
    return float((ranks[labels].sum() - len(positive) * (len(positive) - 1) / 2) / (len(positive) * len(negative)))


def ndcg_at_k(gains_in_rank_order: list[float], ideal: list[float], k: int = 10) -> float:
    def dcg(gains):
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))

    ideal_dcg = dcg(sorted(ideal, reverse=True))
    return dcg(gains_in_rank_order) / ideal_dcg if ideal_dcg > 0 else float("nan")


def evaluate(name: str, rows: list[dict], scores: np.ndarray) -> dict:
    overall = np.array([r["overall"] for r in rows])
    relevance = np.array([r["relevance"] for r in rows])

    by_query = defaultdict(list)
    for i, row in enumerate(rows):
        by_query[row["query"]].append(i)

    per_query_rho, ndcgs, random_ndcgs = [], [], []
    rng = np.random.default_rng(42)
    for indexes in by_query.values():
        idx = np.array(indexes)
        if len(idx) >= 5:
            rho = spearman(scores[idx], overall[idx])
            if not math.isnan(rho):
                per_query_rho.append(rho)
        gains = overall[idx]
        judge_order = idx[np.argsort(-scores[idx])]
        ndcg = ndcg_at_k(list(overall[judge_order]), list(gains))
        if not math.isnan(ndcg):
            ndcgs.append(ndcg)
            random_ndcgs.append(ndcg_at_k(list(rng.permutation(gains)), list(gains)))

    # source-level agreement on Super Search rows (what the bandit consumes)
    source_rows = defaultdict(lambda: defaultdict(list))
    for i, row in enumerate(rows):
        if row["ss_source"] and "supersearch" in row["pools"]:
            source_rows[row["query"]][row["ss_source"]].append(i)
    within_query_rho, best_agree, eligible = [], 0, 0
    for query, sources in source_rows.items():
        aggregates = {
            s: (float(np.mean(scores[np.array(ix)])), float(np.mean(overall[np.array(ix)])))
            for s, ix in sources.items()
            if len(ix) >= 2
        }
        if len(aggregates) < 2:
            continue
        eligible += 1
        judge_means = [v[0] for v in aggregates.values()]
        llm_means = [v[1] for v in aggregates.values()]
        rho = spearman(judge_means, llm_means)
        if not math.isnan(rho):
            within_query_rho.append(rho)
        if int(np.argmax(judge_means)) == int(np.argmax(llm_means)):
            best_agree += 1

    return {
        "judge": name,
        "spearman_overall": round(spearman(scores, overall), 3),
        "spearman_relevance": round(spearman(scores, relevance), 3),
        "per_query_spearman": round(float(np.mean(per_query_rho)), 3),
        "auc_rel2": round(roc_auc(relevance >= 2, scores), 3),
        "ndcg@10": round(float(np.mean(ndcgs)), 3),
        "ndcg@10_random": round(float(np.mean(random_ndcgs)), 3),
        "source_within_query_spearman": round(float(np.mean(within_query_rho)), 3) if within_query_rho else None,
        "source_best_agree@1": round(best_agree / eligible, 3) if eligible else None,
        "source_eligible_queries": eligible,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--judges",
        default=None,
        help=f"comma-separated subset of {ALL_JUDGES} and any registered ft_* judges (default: all)",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        action="append",
        default=[],
        help="fine-tuned model artifact dir (repeatable); registers ft_<run>[,_int8,_torch] judges",
    )
    parser.add_argument(
        "--eval-manifest",
        type=Path,
        default=None,
        help="eval_manifest.json: compute metrics on its held-out rows only (results_heldout.json)",
    )
    args = parser.parse_args()

    finetuned = [name for model_dir in args.model_dir for name in register_finetuned(model_dir)]
    judges = args.judges.split(",") if args.judges else list(ALL_JUDGES) + finetuned

    rows = load_dataset()
    print(f"dataset: {len(rows)} rows, {len({r['query'] for r in rows})} queries")

    eval_rows, eval_slice = rows, slice(None)
    results_file = "results.json"
    if args.eval_manifest:
        manifest = json.loads(args.eval_manifest.read_text())
        eval_slice = np.array(manifest["llm_eval_row_indexes"])
        eval_rows = [rows[i] for i in eval_slice]
        results_file = "results_heldout.json"
        print(
            f"held-out eval: {len(eval_rows)} rows, "
            f"{len({r['query'] for r in eval_rows})} queries "
            f"({manifest['source_eligible_eval_queries']} source-eligible)"
        )
    elif finetuned:
        parser.error(
            "fine-tuned judges must be evaluated with --eval-manifest (their training saw the non-held-out queries)"
        )

    results = []
    for name in judges:
        scores = judge_scores(name.strip(), rows)
        results.append(evaluate(name.strip(), eval_rows, scores[eval_slice]))
        print(json.dumps(results[-1]))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / results_file).write_text(json.dumps(results, indent=2))
    print("\n=== bake-off results ===")
    keys = list(results[0].keys())
    print(" | ".join(f"{k:>28s}" if i == 0 else k for i, k in enumerate(keys)))
    for result in results:
        print(" | ".join(f"{str(result[k]):>28s}" if i == 0 else str(result[k]) for i, k in enumerate(keys)))


if __name__ == "__main__":
    main()
