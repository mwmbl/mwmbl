# Combined Search quality: handover

2026-09-24 · written after PR #448 (provider features) and PR #449 (latency) · the previous
handover is `combined-search-handover.md`, next to this file.

**Goal:** match or beat Brave's API on search quality.

**Where things stand:** the shipped ranking beats calling Staan alone on the judge but still
trails Brave on both measures. The most promising next step is keeping Staan's own order and
letting the index only fill the gaps. On the eval set that beats the shipped ranking on both
measures, but it still needs confirming on fresh queries.

## Current numbers

The eval set is 298 queries, a seed-42 5% sample of the gold test set (see the previous
handover for the method).

| Arm | Gold NDCG | Judge NDCG@10 |
|---|---|---|
| brave | 0.757 | 0.865 |
| **combined (shipped)** | 0.682 | 0.774 |
| staan | 0.734 | 0.756 |

- **Gold** is agreement with scraped Google results. It is independent of our models.
- **The judge** is the local MiniLM cross-encoder `minilm-both-v1`. Its correlation with
  curators is 0.40 on held-out data.
- **The judge can't evaluate anything MiniLM ranks.** Any MiniLM re-rank would be grading
  itself. For those experiments, use gold plus a Haiku pass (see "Tools").
- **Missing results count against an arm on the judge.** `judge_ndcg` scores the list as
  given, so an arm returning fewer than 10 results scores lower. Staan returns 8.8 results on
  average, which is part of its judge deficit.

## Findings this session

All of these are offline, from the saved per-query eval rows. None needed API calls.

### 1. MiniLM as a final re-ranking stage hurts gold. Not recommended.

Re-sorting the shipped top 10 by MiniLM's score (`analysis/rerank.py`):

| Ordering | Gold NDCG | vs shipped, 95% CI |
|---|---|---|
| shipped combined | 0.682 | — |
| MiniLM re-sort of top 10 | 0.627 | −0.056 [−0.082, −0.030] |
| MiniLM re-sort of combined ∪ Staan | 0.621 | −0.061 [−0.088, −0.031] |
| MiniLM score minus 0.03 × LTR position | 0.669 | −0.013 [−0.035, +0.008] |

- **Likely cause:** the gold gain in #448 came from Staan's rank as a feature, and Staan's
  order tracks Google's. A final MiniLM stage throws that signal away.
- **Cost:** 120–450 ms of CPU for 15–20 candidates of about 80 tokens (`analysis/bench.py`,
  fp32 ONNX, 1–4 threads on the dev machine), and 0.7–1.5 s at its 256-token maximum.
- **If MiniLM is tried again, use it as an LTR feature, not a final stage,** so the model
  still sees Staan's rank. It would still need gold plus Haiku to evaluate.

### 2. Skipping Staan when the index looks good enough doesn't work.

This used the older run's rows (`rows-0.05-handover.json`), because only that run has an
index-only arm. It compares against the old combined model.

- Index-only judge NDCG is 0.39, against 0.69 for the old combined.
- **Even a perfect oracle** could skip Staan on only 7.7% of queries without losing quality.
- **Realistic gates on the index's top-3 judge score:**

| Threshold | Queries skipped | Overall judge loss | Loss on skipped queries |
|---|---|---|---|
| ≥ 0.9 | 18% | 0.034 | 0.19 |
| ≥ 0.8 | 36% | 0.074 | 0.21 |

  (Script: `analysis/gate.py`.)
- The index alone is far too weak for this to pay off in quality or latency.

### 3. Staan first, index fills the gaps: the most promising lead

The verify log (`verify-shipped.log`) shows Mwmbl's own results in the shipped top 10 have a
mean judge score of **0.38**. The Staan results they push out score **0.60**. The model is
promoting index results over better Staan ones.

Simulated in `analysis/rerank.py`, on the same 298 queries:

| Ordering | Gold | Judge |
|---|---|---|
| shipped combined | 0.682 | 0.774 |
| staan | 0.734 | 0.756 |
| **Staan first, then combined's other results up to 10** | **0.735** | **0.792** |
| As above, but combined's best index result forced to slot 3 | 0.737 | 0.792 |
| brave | 0.757 | 0.865 |

**Caveats, in order of importance:**

1. **It was found on the eval set.** The rule has no fitted parameters, so overfitting risk is
   low, but it needs confirming on a fresh sample before shipping.
2. **Much of the judge gain probably comes from filling to 10 results.** Staan-first is about
   gold parity with Staan, plus padding. That's still a real product win, since users get 10
   results, but it isn't the model finding better pages.
3. **The judge numbers are MiniLM's.** A Haiku pass would firm them up.
4. **Its ceiling is Staan's.** It doesn't close the gap to Brave (−0.02 gold, −0.07 judge);
   that gap is Staan's own quality.

## Suggested next steps

1. **Add a `staan-first` arm** to `mwmbl/rankeval/evaluation/compare_combined_providers.py`:
   Staan's order, then the combined ranking's non-Staan results, deduped by URL, up to 10.
   - Run it on a **fresh sample**: a different seed or fraction of the gold test set, excluding
     the 298 queries.
   - That costs one Staan call per query. The external cache has a one-week TTL, so the old
     298 queries' Staan results may have expired too.
2. **Haiku pass over the pooled top 10s** of `staan`, `combined` and `staan-first`, using
   `scripts/llm_relabel_pass3_judge.py`: `--dump-batch`, then Haiku subagents, then `--merge`.
   Report NDCG from the Haiku grades next to the MiniLM judge.
3. **If Staan-first holds up, decide how to serve it.** Either:
   - add it as a rule in the endpoint (`mwmbl/tinysearchengine/combined_search.py`), or
   - retrain the LTR model with a monotone constraint or a stronger prior on `staan_rank`, so
     the model learns it rather than a hard rule overriding it.
4. **Understand the gap to Brave, which is now the real target.** Take a per-query breakdown of
   where Brave wins on gold and the judge: navigational queries, news, long tail, or queries
   where Staan returns fewer than 10 results. Then check whether any of those are fixable on
   our side, for example with index results for navigational queries or curation.
5. **Park:** MiniLM as a final stage (finding 1) and gating Staan off (finding 2).

## Data and tools

- **Eval rows** in `devdata/combined_providers_eval/`:
  - `rows-0.05.json`: the verify run, with arms `staan`, `combined` (shipped), `previous`,
    `reference` and `brave`. Each row holds `query`, `gold` (url→weight), `lists` (arm→urls)
    and `judged` (url→MiniLM score for every pooled URL).
  - `rows-0.05-handover.json`: the older run, with arms `staan`, `staan+wiki`, `combined` (old
    model), `mwmbl` (index + Wikipedia) and `brave`.
- **Analysis scripts** in `devdata/combined_providers_eval/analysis/` (untracked). Run them
  from the repo root with `.venv/bin/python`:
  - `rerank.py`: findings 1 and 3.
  - `gate.py`: finding 2.
  - `bench.py`: the MiniLM latency benchmark.
- **Metrics:** `gold_ndcg` and `judge_ndcg` in `compare_combined_providers.py`. The scripts
  above re-implement them identically.
- **Running the eval:** see "How to reproduce" in `combined-search-handover.md`.
  - `.env` isn't loaded automatically; without it, Staan silently returns nothing.
  - After `maturin develop`, use `uv run --no-sync`.

## Production notes

- **Production didn't call Staan until 2026-09-24,** because `STAAN_SEARCH_API_KEY` was
  missing from its environment. Before that date, production Combined Search served
  index-only results for any query beta hadn't already cached. Discount any production
  quality data or user feedback from before then.
- **The search API key's Combined Search quota is 100 a month.** It stood at 89 used on
  2026-09-24. Live checks against the endpoint are expensive; do evaluation offline.
- **Latency is parked.** #449 overlaps the index lookup with the Staan fetch. Both beta and
  production sit at about 1.2–1.3 s against Brave's 1.0 s, and that gap is Staan's own
  response time.
- **Issue #450:** production JSON APIs send a gzip header with a plain-JSON body. It affects
  any live-testing client that asks for compression. Send `Accept-Encoding: identity` until
  it's fixed.
