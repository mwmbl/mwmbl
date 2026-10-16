#!/usr/bin/env python3
"""Verify ONNX export parity for a fine-tuned judge artifact.

Checks, over the 512-row parity sample saved by modal_judge_train.py:
- fp32 ONNX vs torch reference scores: max abs diff < --fp32-tol (default 1e-3)
- int8 ONNX vs fp32 ONNX: Spearman > 0.999 (rank order is what the bandit
  consumes; absolute drift from dynamic quantization is expected)

If int8 fails, serve the fp32 model.

Run:  uv run --with onnxruntime --with tokenizers python \
          scripts/judge_verify_onnx.py --model-dir devdata/judge_train/models/<run>
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from judge_bakeoff import onnx_cross_encoder, spearman


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--fp32-tol", type=float, default=1e-3)
    parser.add_argument("--int8-min-spearman", type=float, default=0.999)
    args = parser.parse_args()

    sample = json.loads((args.model_dir / "parity_sample.json").read_text())
    torch_scores = np.array([row["torch_score"] for row in sample])
    if torch_scores.min() < 0 or torch_scores.max() > 1:
        # CrossEncoder.predict returned raw logits (model has no default
        # activation); the ONNX judge applies sigmoid, so compare on its scale
        torch_scores = 1 / (1 + np.exp(-torch_scores))
    onnx_dir = args.model_dir / "onnx"

    fp32 = onnx_cross_encoder(sample, onnx_dir, "model.onnx", "fp32")
    fp32_diff = float(np.max(np.abs(fp32 - torch_scores)))
    fp32_ok = fp32_diff < args.fp32_tol
    print(
        f"fp32 vs torch: max|diff| {fp32_diff:.2e} "
        f"(spearman {spearman(fp32, torch_scores):.6f}) "
        f"-> {'PASS' if fp32_ok else 'FAIL'}"
    )

    int8_ok = True
    if (onnx_dir / "model.int8.onnx").exists():
        int8 = onnx_cross_encoder(sample, onnx_dir, "model.int8.onnx", "int8")
        rho = spearman(int8, fp32)
        int8_ok = rho > args.int8_min_spearman
        print(
            f"int8 vs fp32: spearman {rho:.6f}, "
            f"max|diff| {float(np.max(np.abs(int8 - fp32))):.2e} "
            f"-> {'PASS' if int8_ok else 'FAIL (serve fp32)'}"
        )
    else:
        print("int8 model not found, skipping")

    sys.exit(0 if fp32_ok and int8_ok else 1)


if __name__ == "__main__":
    main()
