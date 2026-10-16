# Relevance judge fine-tune — results (2026-07-05)

Fine-tuned a cross-encoder judge on Modal (T4, ~2-4 min, <$0.25/run) to replace
the circular bandit reward. Training: sentence-transformers CrossEncoderTrainer,
multi-task — BinaryCrossEntropyLoss on LLM `overall`/10 soft labels +
RankNetLoss on human curation preference pairs, proportional sampling,
checkpoint selection on pairs-val accuracy. Data splits frozen in
`eval_manifest.json` (LLM queries 40/10/50 train/val/eval stratified on
source-eligibility; pair queries 85/5/10; leakage-guarded; 16 pairs/query cap;
top user capped to 30% of train).

## Held-out bake-off (424 LLM queries, 118 source-eligible; results_heldout.json)

| judge | pointwise ρ | per-query ρ | AUC(rel≥2) | NDCG@10 | source ρ | best-src@1 |
|---|---|---|---|---|---|---|
| term_overlap | 0.201 | 0.169 | 0.585 | 0.558 | 0.181 | 0.568 |
| nomic_cosine | 0.355 | 0.334 | 0.693 | 0.617 | 0.161 | 0.568 |
| minilm_ce (zero-shot) | 0.366 | 0.351 | 0.689 | 0.692 | 0.165 | 0.576 |
| jina_turbo_ce (zero-shot) | 0.351 | 0.324 | 0.684 | 0.636 | 0.230 | 0.602 |
| **ft minilm both (WINNER)** | **0.624** | **0.598** | **0.870** | 0.809 | **0.396** | **0.686** |
| ft minilm pairs-only | 0.232 | 0.217 | 0.652 | 0.546 | 0.222 | 0.602 |
| ft minilm pointwise-only | 0.672 | 0.659 | 0.878 | 0.828 | 0.330 | 0.644 |
| ft jina-turbo both | 0.577 | 0.551 | 0.857 | 0.813 | 0.278 | 0.636 |

Paired per-query sign test on source ρ, ft-minilm-both vs jina zero-shot:
22 wins / 11 losses / 82 ties, two-sided p = 0.08 (tie-heavy; every other
metric is decisively better).

## Human agreement on held-out curation pairs (3,551 pairs; results_pairs_eval.json)

| judge | accuracy | add | approve | move | u197 | others |
|---|---|---|---|---|---|---|
| term_overlap | 0.213 | 0.203 | 0.231 | 0.226 | 0.205 | 0.223 |
| nomic_cosine | 0.679 | 0.671 | 0.680 | 0.700 | 0.734 | 0.607 |
| minilm_ce (zero-shot) | 0.671 | 0.666 | 0.662 | 0.694 | 0.729 | 0.595 |
| jina_turbo_ce (zero-shot) | 0.674 | 0.692 | 0.675 | 0.618 | 0.734 | 0.596 |
| **ft minilm both** | **0.866** | 0.889 | 0.850 | 0.812 | 0.888 | 0.838 |
| ft minilm pairs-only | 0.878 | 0.905 | 0.859 | 0.813 | 0.903 | 0.846 |
| ft minilm pointwise-only | 0.670 | 0.665 | 0.690 | 0.669 | 0.688 | 0.647 |
| ft jina-turbo both | 0.865 | 0.883 | 0.836 | 0.839 | 0.897 | 0.824 |

## Conclusions

- **Winner: minilm-both-v1** (cross-encoder/ms-marco-MiniLM-L-6-v2, multi-task).
  Best source-level agreement — the metric the bandit consumes — and passes all
  guardrails (pointwise not regressed vs zero-shot; human agreement 0.866, with
  others-than-top-curator at 0.838, so not single-curator overfit).
- **Multi-task is the win**: single-task runs each win their own axis and
  damage the other (pairs-only collapses pointwise to 0.232; pointwise-only
  leaves human agreement at 0.670 — exactly zero-shot level, i.e. LLM labels
  alone teach nothing about human preference). The human-pairs signal also
  *helps* source-level agreement (0.396 vs 0.330).
- Zero-shot judges all cluster at ~0.67 human agreement; fine-tuning adds
  +19pp overall and +24pp on non-top-curator pairs (0.838 vs ~0.60).
- **Term overlap anti-correlates with human curation** (0.213 pairwise
  accuracy): curators mostly repair navigational queries with official sites
  whose title/extract don't echo the query terms. This is direct evidence the
  term-matchy LTR re-ranker fights curators.
- **Serving**: fp32 O2-optimized ONNX via plain onnxruntime+tokenizers
  (`onnx_cross_encoder()` in scripts/judge_bakeoff.py), sigmoid(logit) in
  [0,1]. Parity vs torch: max|Δ| 4.6e-4, Spearman 0.999998. int8 dynamic
  quantization drifts rank order (Spearman 0.997 vs fp32, below the 0.999 bar)
  → serve fp32; revisit int8 only if CPU cost matters.
- Torch checkpoints live on the Modal volume `judge-train`; local artifacts in
  `devdata/judge_train/models/<run>/` (onnx/, llm_scores.npy,
  pairs_eval_scores.npz, parity_sample.json, train_meta.json).

## Reproduce

```
uv run python scripts/judge_train_prep.py                # frozen splits
uv run --with modal modal run scripts/modal_judge_train.py \
    --base minilm --tasks both --run-name minilm-both-v1  # train + export
uv run --with onnxruntime --with tokenizers python \
    scripts/judge_verify_onnx.py --model-dir devdata/judge_train/models/minilm-both-v1
uv run python scripts/judge_bakeoff.py \
    --eval-manifest devdata/judge_train/eval_manifest.json \
    --model-dir devdata/judge_train/models/minilm-both-v1   # held-out metrics
uv run python scripts/judge_pairs_eval.py \
    --model-dir devdata/judge_train/models/minilm-both-v1   # human agreement
```

## Next

- Swap the bandit reward (`compute_rewards` in
  mwmbl/tinysearchengine/super_search_select/rewards.py) to judge scores.
- Privacy fix: stop persisting query[:512] in SuperSearchImpression /
  SourceProvenance.
- Monitor judge drift per intent/source once online.
