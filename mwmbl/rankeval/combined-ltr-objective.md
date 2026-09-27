# Combined Search: ranking objective and serving-pool labels

2026-09-27 · follows `combined-search-miss-attribution.md` · scripts and data in
`scripts/combined_ltr_labels/` and `devdata/combined_ltr_labels/`

**Question:** without letting Staan set the order, the gap to Brave is mostly ordering. The
best ten of Staan plus the LTR's top 30, perfectly ordered, score 0.903 against the shipped
model's 0.801 and Brave's 0.895. The shipped model puts weak index pages (overall ≤ 3) in its
top ten while good Staan results sit at ranks 11–30. Does a ranking objective on graded
labels fix this where the binary classifier doesn't, and do labels for the pool it actually
serves help?

## Summary

- **`rank:ndcg` beats the binary recipe, offline and end to end.**
  - In cross-validation over the 849 labelled queries it gains about +0.015 NDCG@10.
  - On the held-out en-gb queries, with blind Haiku UK pass-3 judgments, it gains +0.011
    over the shipped model [+0.006, +0.016].
  - `rank:pairwise` lands in between.
- **The 8,082 serving-pool labels help a little.**
  - They add +0.001 to +0.003 in every arm, and are never significant on their own.
  - `rank:ndcg` with them is the best arm: +0.013 over shipped [+0.008, +0.018].
- **The binary retrain reproduces the shipped model**, 0.798 against 0.796. That holds even
  without curation data and with approximate Staan ranks, so the harness is sound.
- **The gain is modest against the gap.** The best arm reaches 0.809. Brave scores 0.892,
  and the ordering ceiling is about 0.90. A better objective alone doesn't close it.
- **Why rank:ndcg may have looked worse before:** a ranking objective's scores are margins,
  mostly negative (60–70% of kept candidates here).
  - `LTRRanker` keeps only `predictions > 0`, so serving such a model as-is would silently
    drop most candidates.
  - `LTRRanker` and the Python pipelines also score filtered documents 0, which ranks them
    above every negative-scored one. That alone costs rank:ndcg 0.013 on the original pool.
  - Every arm here treats the majority-terms filter as an exclusion. At serving,
    `engb_eval.BoosterModel` passes the sigmoid of the margin.

## Cross-validation (5 folds over the 849 queries)

Same features: mwmbl_rank's 50, plus `in_staan` and `staan_rank`. Same tree parameters
(exact, λ 2, η 0.3, depth 6, 100 rounds) and same data: LLM rows plus the extension dataset
at weight 0.25. Only the objective and the `+new` labels differ. Gains are the linear
overall grade.

| Arm | Serving pool NDCG@10 | vs binary | Original pool NDCG@10 | vs binary |
|---|---|---|---|---|
| binary (shipped recipe) | 0.8605 | — | 0.8067 | — |
| binary+new | 0.8630 | +0.0025 [−0.0011, +0.0059] | 0.8073 | +0.0006 [−0.0026, +0.0039] |
| ndcg | 0.8752 | +0.0147 [+0.0109, +0.0187] | 0.8234 | +0.0166 [+0.0124, +0.0211] |
| **ndcg+new** | **0.8761** | **+0.0156 [+0.0117, +0.0194]** | **0.8235** | **+0.0168 [+0.0125, +0.0210]** |
| pairwise | 0.8717 | +0.0112 [+0.0073, +0.0153] | 0.8183 | +0.0115 [+0.0071, +0.0160] |
| pairwise+new | 0.8734 | +0.0129 [+0.0093, +0.0167] | 0.8173 | +0.0105 [+0.0062, +0.0148] |

- The *serving pool* is Staan's results plus the index's top 30 under the shipped model,
  every pair labelled (`pool.json`).
- The *original pool* is the Pass-2 pool that `llm_experiment` evaluates on.

## End to end on en-gb (289 queries)

Each arm, retrained on all 849 queries, ranks the same fresh index retrieval plus Staan's
en-gb results through `CombinedLTRRanker` + MMR. Top-ten URLs nobody had judged were
graded blind by Claude Haiku 4.5 subagents with the UK pass-3 prompt: 591 new judgments and
404 anchors. The ideal is every en-gb pass-3 judgment for the query.

| Arm | NDCG@10 | vs shipped | Weak (≤ 3) in top 10 |
|---|---|---|---|
| shipped | 0.796 | — | 21.0% |
| binary | 0.798 | +0.001 [−0.003, +0.006] | 20.5% |
| binary+new | 0.799 | +0.003 [−0.002, +0.008] | 20.2% |
| ndcg | 0.807 | +0.011 [+0.006, +0.016] | 20.1% |
| **ndcg+new** | **0.809** | **+0.013 [+0.008, +0.018]** | **19.9%** |
| pairwise | 0.805 | +0.008 [+0.003, +0.014] | 19.9% |
| pairwise+new | 0.807 | +0.011 [+0.005, +0.016] | 19.9% |
| brave | 0.892 | +0.096 | 9.8% |

- **Robustness:** the new judgments' anchors run +0.33 generous. Removing that offset from
  every new judgment leaves `ndcg+new` at +0.012 [+0.007, +0.016].
- **Newly judged URLs:** they make up 1.5% of the shipped model's top tens and 6–7% of each
  retrained arm's.

## Caveats

- **The human gate hasn't run.** The curation export (`devdata/judgments_export/`) wasn't
  available, so no arm trains on curation, and the second retrain gate (pair accuracy on
  held-out human curation pairs) wasn't run. Run it before shipping.
- **Approximate Staan ranks.** Where another pool had already added a URL, its Staan rank
  in the LLM training rows is reconstructed, since `pass2_staan.jsonl` wasn't available
  (`objective_experiment.staan_ranks`). The en-gb Staan ranks are exact.
- **Tuning.** Every arm uses the shipped tree parameters. rank:ndcg wasn't tuned, and the
  extension rows' gold grade of 7 is a guess.
- **Judge offsets.** The serving-pool labels are corrected by each judge's shrunk mean
  anchor drift (`README.md` in `scripts/combined_ltr_labels/`).

## What shipping would take

1. **Training.** `mwmbl_rank` trains `binary:logistic` only. Either add the objective
   there, or train in Python and save a booster that the Rust side loads. A loaded
   booster's `predict` returns margins for a ranking objective.
2. **Serving.** Make the filter an explicit exclusion in `XGBPipeline::predict` /
   `LTRRanker.order_results`, or apply a sigmoid. Either way, a negative score must stop
   meaning "drop".
3. **Human gate.** Run the curation pair-accuracy gate with curation data included.

To reproduce:

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/objective_experiment.py
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_eval.py report
```
