# Combined Search: ranking with Jev (TypeSafe)

2026-09-30 · follows `combined-search-haiku-eval.md` (same directory) · the tables are printed
by `python -m mwmbl.rankeval.evaluation.jev_arms_report pointwise listwise`, from data
committed under `devdata/combined_providers_eval/`.

**Question:** can Jev, TypeSafe's structured-judgment model, rank Combined Search's candidates
better than the current best arm (Staan first, remaining slots filled by MiniLM)?

> **Superseded in part (2026-09-30):** judged holistically against the stronger reference
> `staan-first, fill ndcg+new, no MMR`, the pure Jev re-rank below only ties it. Jev's score plus
> a small Staan-rank prior wins, +0.31 [+0.18, +0.44] on the −3..3 scale. See the Jev section of
> `combined-holistic-eval.md`.

## Summary

- **A pointwise Jev re-rank is the best arm we've had:** +0.039 UK relevance and +0.035
  pass-3 overall over the Staan-first MiniLM arm. That closes 60% of the gap to Brave:
  −0.025 [−0.041, −0.009], down from −0.064.
- **Letting Jev override Staan's order is what helps.** Using Jev only to fill the slots after
  Staan gains +0.009. That is the opposite of MiniLM, which lost whenever it overrode Staan.
- **Listwise (one Choice over all candidates) loses to pointwise.** It is level with the
  baseline as a re-rank, and slightly worse as the fill.
- **Cheap and fast:** 0.63 s p50 and 0.77 s p90 for one request per query (about 34
  candidates), roughly $0.0003 a query. That still sits after Staan on the critical path
  unless the index candidates are scored while Staan is in flight.

## Method

- **Queries and candidates:** the 295 en-gb queries of the Haiku eval. For each: Staan's top 10,
  the LTR's top 30 and the Wikipedia pool, with the same title/URL/snippet a judge sees
  (`engb/jev_candidates.json`, a median of 34 per query).
- **Jev (`jev-1.13.0`), one request per query** (`scripts/combined_search_haiku/jev_score.py`):
  - **pointwise:** the query and "a web searcher in the United Kingdom" as the state. One
    Score question per candidate, with the candidate in the question's instructions and the
    Haiku UK-relevance rubric as four levels. The candidate's score is the probability-weighted
    level.
  - **listwise:** every candidate in the state and one Choice, "which of `results` best
    satisfies `query`?". The candidate's score is its probability.
- **Arms:**
  - **fill:** Staan in Staan's order, then the LTR top 30 + Wikipedia sorted by Jev.
  - **re-rank:** Staan + LTR top 30 + Wikipedia, sorted by Jev alone.
- **Judging:** the existing Haiku UK-relevance and pass-3 judgments. The Jev arms put 388
  ungraded URLs into their top tens; these were graded in a new blind pass of Claude Haiku 4.5
  subagents (`make_batches_jev.py`, `consolidate_jev.py`) with 412 anchors mixed in.
- **The harness reproduces the published baseline:** running MiniLM through it as a pseudo-run
  (`--minilm`) gives 0.785 and 0.841, as in the previous report.

## Results

### Haiku UK relevance NDCG@10 (295 queries)

| Arm | Score | vs Staan-first MiniLM fill | vs Brave |
|---|---|---|---|
| combined (shipped) | 0.728 | −0.053 | −0.117 |
| staan | 0.742 | −0.038 | −0.102 |
| staan-first, fill MiniLM(LTR top 30 + Wikipedia) | 0.780 | — | −0.064 [−0.080, −0.049] |
| staan-first, fill Jev pointwise | 0.790 | +0.009 [+0.006, +0.013] | −0.055 |
| **Jev pointwise re-rank** | **0.820** | **+0.039 [+0.029, +0.051]** | **−0.025 [−0.041, −0.009]** |
| staan-first, fill Jev listwise | 0.774 | −0.007 [−0.010, −0.003] | −0.071 |
| Jev listwise re-rank | 0.786 | +0.005 [−0.003, +0.014] | −0.059 |
| brave | 0.844 | +0.064 | — |

### Haiku pass-3 overall NDCG@10 (289 queries)

| Arm | Score | vs Staan-first MiniLM fill | vs Brave |
|---|---|---|---|
| combined (shipped) | 0.793 | −0.040 | −0.093 |
| staan | 0.792 | −0.042 | −0.095 |
| staan-first, fill MiniLM(LTR top 30 + Wikipedia) | 0.834 | — | −0.053 |
| staan-first, fill Jev pointwise | 0.847 | +0.014 [+0.010, +0.017] | −0.039 |
| **Jev pointwise re-rank** | **0.869** | **+0.035 [+0.026, +0.045]** | **−0.018 [−0.030, −0.005]** |
| staan-first, fill Jev listwise | 0.832 | −0.001 | −0.054 |
| Jev listwise re-rank | 0.840 | +0.007 | −0.047 |
| brave | 0.887 | +0.053 | — |

The pointwise re-rank's top ten holds, on average, 7.4 Staan results, 1.9 index results and
0.7 Wikipedia pages.

## How far to trust it

- **Judge drift:** the new Haiku pass grades a little higher than the original on the
  anchors. UK relevance was re-graded identically for 60% of them (mean +0.11); pass-3 overall
  shifted by a mean of +0.45. The newly graded URLs are mostly the Jev arms', so this favours
  Jev. Two checks show the win isn't from drift:
  - **Discounting the new grades:** with every newly graded URL's relevance lowered by the
    anchor shift (0.11), the pointwise re-rank's lead is +0.035. Lowered by a full grade
    (1.0), it is still +0.011.
  - **Original grades only:** on the 93 queries fully covered by the original grades, before
    the new pass, the lead was +0.039 [+0.025, +0.052].
- **Same rubric:** Jev was given the judge's own rubric, so part of the gain may be agreeing
  with Haiku rather than with searchers. Gold NDCG (Google agreement) is the independent check
  not yet run.
- **Found on the eval set:** one prompt design was tried per variant, with no tuning, but the
  result should be confirmed on fresh queries before shipping.

## Next steps

1. **Confirm on fresh queries**, and add gold NDCG for the Jev arms.
2. **Try composite questions:** separate Nouls for authority and for "is this the entity the
   query names" (Staan's two weak spots), combined with relevance in code, plus an ethos
   question.
3. **Latency:** score the index and Wikipedia candidates while Staan is in flight, then Staan's
   ten in a second, smaller request. Or feed Jev's score to the LTR as a feature.
