#!/usr/bin/env python3
"""Fine-tune the relevance judge cross-encoder on Modal (T4, minutes, cents).

Multi-task fine-tune via sentence-transformers CrossEncoderTrainer:
- pointwise: BinaryCrossEntropyLoss on LLM `overall` soft labels in [0,1]
  (matches serve-time sigmoid(logit) calibration)
- pairs: RankNetLoss on human curation preference pairs (docs=[pos,neg]);
  the pairs say "pos > neg", not "neg is bad"

Checkpoint selection on pairs-val accuracy (closest proxy to the bandit's
source-level objective). After training the job also:
- scores the FULL LLM dataset (row order = judge_bakeoff.load_dataset, so the
  array can be dropped straight into devdata/judge_bakeoff/ as a score cache)
- scores the held-out preference pairs (pairs_eval pos/neg)
- exports ONNX (O2-optimized fp32 + dynamic-int8) for CPU serving

Artifacts land locally in devdata/judge_train/models/{run_name}/:
  onnx/ (model.onnx, model_quantized.onnx, tokenizer files), llm_scores.npy,
  pairs_eval_scores.npz, parity_sample.json, train_meta.json
Full torch checkpoint stays on the Modal volume `judge-train` (/ckpt).

One-time auth:  uv run --with modal modal token new
Run:  uv run --with modal modal run scripts/modal_judge_train.py \
          --base minilm --tasks both --run-name minilm-both-v1
"""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import modal

BASES = {
    "minilm": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "jina-turbo": "jinaai/jina-reranker-v1-turbo-en",
}
MAX_LENGTH = 256
LLM_DATASET = "devdata/rankeval-2026-04/learning-to-rank-llm.csv.gz"

app = modal.App("judge-train")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch", "sentence-transformers>=4.1", "transformers", "accelerate", "datasets", "optimum[onnxruntime]>=1.24"
    )
    .add_local_dir("devdata/judge_train", "/repo/devdata/judge_train", ignore=["models/**"])
    .add_local_file(LLM_DATASET, f"/repo/{LLM_DATASET}")
    .add_local_file("scripts/judge_bakeoff.py", "/repo/scripts/judge_bakeoff.py")
)

volume = modal.Volume.from_name("judge-train", create_if_missing=True)


def load_jsonl_gz(path):
    import gzip

    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


@app.function(image=image, gpu="T4", timeout=3600, volumes={"/ckpt": volume})
def train(
    base: str,
    tasks: str,
    run_name: str,
    epochs: float,
    lr: float,
    batch_size: int,
    eval_steps: int,
    seed: int,
    git_sha: str,
) -> bytes:
    import os
    import sys
    import time

    import numpy as np
    import torch
    from datasets import Dataset
    from sentence_transformers.cross_encoder import CrossEncoder, CrossEncoderTrainer, CrossEncoderTrainingArguments
    from sentence_transformers.cross_encoder.losses import BinaryCrossEntropyLoss, RankNetLoss
    from sentence_transformers.evaluation import SentenceEvaluator
    from sentence_transformers.training_args import MultiDatasetBatchSamplers
    from transformers import set_seed

    os.chdir("/repo")
    sys.path.insert(0, "scripts")
    import judge_bakeoff

    set_seed(seed)
    started = time.time()
    data_dir = Path("devdata/judge_train")

    pointwise_train = load_jsonl_gz(data_dir / "pointwise_train.jsonl.gz")
    pointwise_val = load_jsonl_gz(data_dir / "pointwise_val.jsonl.gz")
    pairs_train = load_jsonl_gz(data_dir / "pairs_train.jsonl.gz")
    pairs_val = load_jsonl_gz(data_dir / "pairs_val.jsonl.gz")
    pairs_eval = load_jsonl_gz(data_dir / "pairs_eval.jsonl.gz")

    train_datasets, train_losses = {}, {}
    trust = base not in ("cross-encoder/ms-marco-MiniLM-L-6-v2",)
    model = CrossEncoder(base, trust_remote_code=trust)
    model.max_length = MAX_LENGTH
    if tasks in ("both", "pointwise"):
        train_datasets["pointwise"] = Dataset.from_list(
            [{"query": r["query"], "doc": r["doc_text"], "label": float(r["label"])} for r in pointwise_train]
        )
        train_losses["pointwise"] = BinaryCrossEntropyLoss(model)
    if tasks in ("both", "pairs"):
        train_datasets["pairs"] = Dataset.from_list(
            [{"query": r["query"], "docs": [r["pos"], r["neg"]], "labels": [1.0, 0.0]} for r in pairs_train]
        )
        train_losses["pairs"] = RankNetLoss(model)

    class JudgeEvaluator(SentenceEvaluator):
        """Pairs-val accuracy (primary) + pointwise-val Spearman."""

        def __init__(self):
            super().__init__()
            self.primary_metric = "pairs_accuracy"
            self.pair_inputs = [(p["query"], p["pos"]) for p in pairs_val] + [(p["query"], p["neg"]) for p in pairs_val]
            self.point_inputs = [(r["query"], r["doc_text"]) for r in pointwise_val]
            self.point_labels = np.array([r["label"] for r in pointwise_val])

        def __call__(self, model, output_path=None, epoch=-1, steps=-1):
            scores = model.predict(self.pair_inputs, batch_size=256, show_progress_bar=False)
            half = len(pairs_val)
            accuracy = float(np.mean(scores[:half] > scores[half:]))
            point = model.predict(self.point_inputs, batch_size=256, show_progress_bar=False)
            rho = judge_bakeoff.spearman(point, self.point_labels)
            metrics = {"pairs_accuracy": accuracy, "pointwise_spearman": rho}
            print(f"eval @ step {steps}: {metrics}", flush=True)
            return metrics

    evaluator = JudgeEvaluator()
    baseline = evaluator(model, steps=0)

    args = CrossEncoderTrainingArguments(
        output_dir=f"/ckpt/{run_name}/checkpoints",
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        learning_rate=lr,
        warmup_ratio=0.1,
        weight_decay=0.01,
        fp16=True,
        seed=seed,
        eval_strategy="steps",
        eval_steps=eval_steps,
        save_strategy="steps",
        save_steps=eval_steps,
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="pairs_accuracy",
        greater_is_better=True,
        logging_steps=50,
        multi_dataset_batch_sampler=MultiDatasetBatchSamplers.PROPORTIONAL,
        report_to="none",
    )
    trainer = CrossEncoderTrainer(
        model=model, args=args, train_dataset=train_datasets, loss=train_losses, evaluator=evaluator
    )
    trainer.train()
    final = evaluator(model, steps=-1)

    final_dir = f"/ckpt/{run_name}/final"
    model.save_pretrained(final_dir)

    # --- reference scores with the final torch model --------------------------
    artifact_dir = Path(f"/tmp/artifact/{run_name}")
    artifact_dir.mkdir(parents=True)
    rows = judge_bakeoff.load_dataset()
    llm_scores = model.predict([(r["query"], r["doc_text"]) for r in rows], batch_size=256, show_progress_bar=False)
    np.save(artifact_dir / "llm_scores.npy", np.asarray(llm_scores, dtype=np.float64))
    pos = model.predict([(p["query"], p["pos"]) for p in pairs_eval], batch_size=256, show_progress_bar=False)
    neg = model.predict([(p["query"], p["neg"]) for p in pairs_eval], batch_size=256, show_progress_bar=False)
    np.savez(artifact_dir / "pairs_eval_scores.npz", pos=pos, neg=neg)

    parity_indexes = np.random.default_rng(0).choice(len(rows), 512, replace=False)
    parity = [
        {"query": rows[i]["query"], "doc_text": rows[i]["doc_text"], "torch_score": float(llm_scores[i])}
        for i in parity_indexes
    ]
    (artifact_dir / "parity_sample.json").write_text(json.dumps(parity))

    # --- ONNX export: fp32 (O2-optimized) + dynamic int8 ----------------------
    onnx_error = None
    try:
        from optimum.onnxruntime import ORTModelForSequenceClassification, ORTOptimizer, ORTQuantizer
        from optimum.onnxruntime.configuration import AutoQuantizationConfig, OptimizationConfig

        onnx_dir = artifact_dir / "onnx"
        ort_model = ORTModelForSequenceClassification.from_pretrained(final_dir, export=True)
        ort_model.save_pretrained(onnx_dir)  # raw model.onnx
        # quantize from the raw export — the O2-optimized fused graph breaks
        # onnx shape inference inside the quantizer
        quantizer = ORTQuantizer.from_pretrained(onnx_dir, file_name="model.onnx")
        quantizer.quantize(
            save_dir=onnx_dir, quantization_config=AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
        )
        (onnx_dir / "model_quantized.onnx").rename(onnx_dir / "model.int8.onnx")
        optimizer = ORTOptimizer.from_pretrained(ort_model)
        optimizer.optimize(save_dir=onnx_dir, optimization_config=OptimizationConfig(optimization_level=2))
        (onnx_dir / "model_optimized.onnx").rename(onnx_dir / "model.onnx")
        model.tokenizer.save_pretrained(str(onnx_dir))
    except Exception as exc:  # jina custom code may not export; keep torch arm
        onnx_error = f"{type(exc).__name__}: {exc}"
        print(f"ONNX export FAILED (torch scores still usable): {onnx_error}", flush=True)

    meta = {
        "run_name": run_name,
        "base": base,
        "tasks": tasks,
        "epochs": epochs,
        "lr": lr,
        "batch_size": batch_size,
        "eval_steps": eval_steps,
        "seed": seed,
        "max_length": MAX_LENGTH,
        "git_sha": git_sha,
        "train_pairs": len(pairs_train),
        "train_pointwise": len(pointwise_train),
        "zero_shot_val": baseline,
        "final_val": final,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "best_metric": trainer.state.best_metric,
        "onnx_error": onnx_error,
        "train_seconds": round(time.time() - started, 1),
        "torch_version": torch.__version__,
    }
    (artifact_dir / "train_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2), flush=True)
    volume.commit()

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(artifact_dir, arcname=".")
    return buffer.getvalue()


@app.local_entrypoint()
def main(
    base: str = "minilm",
    tasks: str = "both",
    run_name: str = None,
    epochs: float = 2.0,
    lr: float = 2e-5,
    batch_size: int = 64,
    eval_steps: int = 200,
    seed: int = 42,
):
    assert base in BASES, f"--base must be one of {sorted(BASES)}"
    assert tasks in ("both", "pairs", "pointwise")
    run_name = run_name or f"{base}-{tasks}-v1"
    git_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()

    artifact = train.remote(BASES[base], tasks, run_name, epochs, lr, batch_size, eval_steps, seed, git_sha)

    out_dir = Path("devdata/judge_train/models") / run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as tar:
        tar.extractall(out_dir)
    meta = json.loads((out_dir / "train_meta.json").read_text())
    print(f"\nartifacts -> {out_dir}")
    print(f"val: zero-shot {meta['zero_shot_val']} -> final {meta['final_val']}")
    if meta["onnx_error"]:
        print(f"WARNING: ONNX export failed: {meta['onnx_error']}")
