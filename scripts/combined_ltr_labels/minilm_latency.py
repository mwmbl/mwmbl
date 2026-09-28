"""Latency of scoring every Combined Search candidate with the served MiniLM judge.

Serving `ndcg+new+both` means the judge scores every record `CombinedLTRRanker` passes to
the model, before the LTR runs. This replays the en-gb retrieval of `engb_eval.py` through
the shipped ranker, captures those records per query, and times on them:

- the shipped LTR's `predict` and the whole `search_retrieved`, for scale;
- `Judge.score` on the same records, at several onnxruntime intra-op thread counts;
- the judge on only the LTR's top k, the cost of a two-stage alternative.

    capture  retrieve and rank each query once -> devdata/combined_ltr_labels/latency_records.json
    time     time the judge on the captured records

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys
import time
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import onnxruntime
from django.conf import settings
from tokenizers import Tokenizer

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.super_search_select.judge import MAX_TOKENS, Judge, doc_text
from scripts.combined_ltr_labels.engb_eval import ENGB, LABELS, staan_documents

RECORDS = LABELS / "latency_records.json"
JUDGE_DIR = Path(settings.SUPER_SEARCH_JUDGE_MODEL_DIR)
THREADS = [1, 2, 4, 0]  # 0 is onnxruntime's default: one thread per physical core
TOP_KS = [10, 20, 30]
REPEATS = 3


class TimedModel:
    """The shipped model, keeping each query's records and how long predict took."""

    def __init__(self, model):
        self.model = model
        self.calls: list[tuple[list[dict], float]] = []

    def predict(self, records: list[dict]) -> np.ndarray:
        start = time.perf_counter()
        predictions = self.model.predict(records)
        self.calls.append((records, time.perf_counter() - start))
        return predictions


def capture():
    model = TimedModel(RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH)))
    ranker = MMRRanker(CombinedLTRRanker(RemoteIndex(), DummyCompleter(), model))
    rows = json.loads((ENGB / "rows-0.05.json").read_text())
    text = json.loads((ENGB / "pool_text.json").read_text())
    out = []
    for i, row in enumerate(rows):
        query = row["query"]
        retrieval = ranker.retrieve(query)
        staan = staan_documents(row, text[query])
        start = time.perf_counter()
        ranked = ranker.search_retrieved(retrieval, staan)
        rank_seconds = time.perf_counter() - start
        records, predict_seconds = model.calls[-1]
        predictions = model.model.predict(records)
        top = [int(j) for j in np.argsort(predictions)[::-1]]
        out.append(
            {
                "query": query,
                "docs": [doc_text(r["title"], r["extract"]) for r in records],
                "ltr_order": top,
                "predict_seconds": predict_seconds,
                "rank_seconds": rank_seconds,
                "results": len(ranked),
            }
        )
        if i % 25 == 0:
            print(i, query, len(records), flush=True)
    RECORDS.write_text(json.dumps(out))


class ThreadedJudge(Judge):
    def __init__(self, model_dir: Path, threads: int):
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(
            str(model_dir / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}


def best_of(judge: Judge, query: str, docs: list[str]) -> float:
    if not docs:
        return 0.0
    seconds = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        judge.score(query, docs)
        seconds.append(time.perf_counter() - start)
    return min(seconds)


def summary(name: str, values: list[float]):
    ms = np.array(values) * 1000
    p50, p90, p99 = np.percentile(ms, [50, 90, 99])
    print(f"{name:34s} mean {ms.mean():7.1f}  p50 {p50:7.1f}  p90 {p90:7.1f}  p99 {p99:7.1f}  max {ms.max():7.1f} ms")


def time_judge():
    queries = json.loads(RECORDS.read_text())
    counts = np.array([len(q["docs"]) for q in queries])
    print(
        f"{len(queries)} queries; candidates per query: mean {counts.mean():.1f}, "
        f"p50 {np.percentile(counts, 50):.0f}, p90 {np.percentile(counts, 90):.0f}, max {counts.max()}"
    )
    tokenizer = Tokenizer.from_file(str(JUDGE_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    lengths = np.array([len(tokenizer.encode(q["query"], d).ids) for q in queries for d in q["docs"]])
    print(
        f"tokens per pair: mean {lengths.mean():.0f}, p50 {np.percentile(lengths, 50):.0f}, "
        f"at the {MAX_TOKENS} cap {(lengths >= MAX_TOKENS).mean():.0%}"
    )
    summary("shipped LTR predict", [q["predict_seconds"] for q in queries])
    summary("shipped search_retrieved", [q["rank_seconds"] for q in queries])

    per_candidate = {}
    for threads in THREADS:
        judge = ThreadedJudge(JUDGE_DIR, threads)
        judge.score("warm up", ["warm up"] * 8)
        seconds = [best_of(judge, q["query"], q["docs"]) for q in queries]
        summary(f"judge, all candidates, {threads or 'default'} threads", seconds)
        per_candidate[threads] = sum(seconds) / counts.sum()
        if threads == 1:
            for k in TOP_KS:
                top_seconds = [best_of(judge, q["query"], [q["docs"][j] for j in q["ltr_order"][:k]]) for q in queries]
                summary(f"judge, LTR top {k}, 1 thread", top_seconds)
    for threads, seconds in per_candidate.items():
        print(f"{threads or 'default'} threads: {seconds * 1000:.1f} ms a candidate, {1 / seconds:.0f} a second")


if __name__ == "__main__":
    {"capture": capture, "time": time_judge}[sys.argv[1]]()
