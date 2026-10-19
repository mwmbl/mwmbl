# Retraining the query-time model on the crawl-domain index's pools

2026-10-19 · follows `index-write-order-crawl-model.md` (same directory, PR #463). The NDCG
and recovery tables come from `scripts/index_write_order/evaluate.py report`, which reads
only committed data. The attribution table needs the local indexes.

**Question:** #463 found that the crawl-domain write-time order keeps more good pages than the
heuristic, but loses end to end (−0.019 NDCG@10). The shipped query-time model outranks the
pages it keeps. That model was trained only on pools from the heuristic's index. The
hypothesis was that retraining the query-time model on pools drawn from the crawl-domain
index would make crawl-domain a net win. Does it?

## Summary

- **No: retraining helps both orders, and the heuristic still edges ahead.** Each index ranked
  by a query model retrained on pools from its own index:

  | Write-time order | Query model | Haiku NDCG@10 |
  |---|---|---|
  | heuristic | shipped | 0.346 |
  | crawl-domain | shipped | 0.327 |
  | heuristic | + heuristic pools | **0.363** |
  | crawl-domain | + crawl-domain pools | 0.357 |

  crawl-domain minus heuristic, each with its own retrained model, is −0.006 [−0.017, +0.005].
  The gap shrinks from −0.018 and the interval now includes zero, but it doesn't flip.
- **crawl-domain does gain more from retraining:** +0.030 from its own pools, against +0.017
  for the heuristic from its own. That fits the distribution-shift reading of #463, but the
  shift isn't large enough to overturn the result.
- **Each retrained model suits the index its pools came from.** Swapping them loses on both
  indexes:
  - the heuristic index with the crawl-domain-pool model scores −0.009 [−0.016, −0.002]
    against its own model;
  - the crawl-domain index with the heuristic-pool model scores 0.353, below 0.357.
- **The outranking isn't fixed.** Of the good results that only the heuristic arm ranks in its
  top ten, the crawl-domain arm still holds 219 and ranks others above them (#463: 166). It is
  still the better write order at write time: the heuristic evicts 203 of crawl-domain's good
  results, against 131 the other way.
- **Separately, more index-only pool labels help the query model on their own.** The heuristic
  index with the model retrained on its own pools scores +0.017 [+0.011, +0.024] over the
  shipped model. Spam-blog hosts in its top tens halve, from 35 to 18. This is the actionable
  result, with the caveat below about Staan.

**Recommendation:** keep `INDEX_PAGE_RANKER=heuristic`. A full index rewrite with the crawl
model isn't justified by this. Worth pursuing instead: retrain the shipped combined model with
index-only pool labels, after checking that it doesn't cost anything with Staan in the pool.

## Method

- **Training index.** The combined model trains on 849 LLM-labelled queries, none of which is
  an eval query. `build_corpus.py` and `build_index.py`, with `INDEX_WRITE_ORDER_QUERIES=train`,
  build the same kind of corpus as #456 for their lookup terms:
  - the 2023–24 crawl batches;
  - the live index's pages for those terms.

  That comes to 8.9M documents over 2,618 terms. The corpus is written once with the
  heuristic order and once with crawl-domain, each into its own index.
- **Pools.** For each training query and each index, `retrain.py pool` keeps the top 20 by the
  shipped combined model, index-only, plus the top 20 by the heuristic. This mirrors how
  Pass 2 pooled the production index.
- **Labels.** The 21,147 pooled (query, URL) pairs without a Pass-3 judgment were judged blind
  by Claude Haiku 4.5 with the Pass-3 prompt (relevance, ethos and overall). 20.6% scored
  overall ≥ 4.
- **Models.** Each model uses the combined model's training rows and config unchanged
  (`provider_features.py`), plus one index's pooled rows that the frame didn't already hold.
  Rows are labelled as the LLM rows are (overall ≥ 4, weight 1). They are marked as Staan not
  asked, which is how the A/B ranks.
  - The heuristic pools add 8,217 rows (24.6% positive).
  - The crawl-domain pools add 14,178 rows (20.3% positive).

  With no pool rows added, `retrain.py train` reproduces the shipped model's predictions
  exactly. So the differences come only from the added rows.
- **A/B.** #463's eval indexes and harness: 269 en-gb queries, index-only, with MMR, judged
  blind by Claude Haiku 4.5 with the UK relevance prompt. 1,265 new URLs needed grades.

## Results

Haiku NDCG@10, 269 queries. The heuristic and crawl-domain rows are #463's arms with the
shipped model.

| Write-time order | Query model | NDCG@10 | vs heuristic, shipped | Good results per query |
|---|---|---|---|---|
| heuristic | shipped | 0.346 | | 3.74 |
| crawl-domain | shipped | 0.327 | −0.018 [−0.031, −0.006] | 3.65 |
| heuristic | + heuristic pools | 0.363 | +0.017 [+0.011, +0.024] | 3.89 |
| heuristic | + crawl-domain pools | 0.354 | +0.009 [+0.001, +0.016] | 3.78 |
| crawl-domain | + heuristic pools | 0.353 | +0.008 [−0.004, +0.020] | 3.79 |
| crawl-domain | + crawl-domain pools | 0.357 | +0.011 [−0.001, +0.023] | 3.75 |

Against the heuristic with its own retrained model:

| | NDCG@10 difference |
|---|---|
| crawl-domain, + crawl-domain pools | −0.006 [−0.017, +0.005] |
| crawl-domain, + heuristic pools | −0.009 [−0.020, +0.001] |
| heuristic, + crawl-domain pools | −0.009 [−0.016, −0.002] |

Brave's results graded ≥2 (gain-weighted share in the top ten):

| Write-time order | Query model | All good (2,598) | Evicted targets (465) | Controls (101) |
|---|---|---|---|---|
| heuristic | shipped | 9.2% | 16.1% | 80.6% |
| crawl-domain | shipped | 8.9% | 20.0% | 64.7% |
| heuristic | + heuristic pools | 9.1% | 15.0% | 82.1% |
| crawl-domain | + crawl-domain pools | **10.1%** | **24.8%** | 69.4% |

By Brave's labels, crawl-domain with its retrained model is the best arm overall. It
recovers the most evicted targets, but it gives up some of the controls production already
ranks. Haiku NDCG, which pools every arm's top ten, still puts it slightly below the
heuristic.

Where good results went missing, each arm with its own retrained model:

| Good results in the top ten of | missing from the top ten of | evicted there | kept there, outranked |
|---|---|---|---|
| heuristic, + heuristic pools | crawl-domain, + crawl-domain pools | 131 | 219 |
| crawl-domain, + crawl-domain pools | heuristic, + heuristic pools | 203 | 108 |

Top-ten results whose first host label is numbered (`crawl_model.NUMBERED_LABEL`). This test
counts a few more than #463's table did, so the counts are comparable only within this table.

| Write-time order | shipped | + heuristic pools | + crawl-domain pools |
|---|---|---|---|
| heuristic | 35 | 18 | 14 |
| crawl-domain | 32 | 20 | 15 |

## Caveats and next steps

- **The new rows are index-only.** They are marked "Staan not asked", like the A/B, so they
  shape only the model's index-only branch. With Staan in the pool, as Combined Search
  actually runs, the +0.017 needs checking. Run `compare_combined_providers` or the holistic
  eval with the heuristic-pool model before shipping it.
- **The crawl-domain write order is in-sample on these training queries.** The crawl model's
  positives include the combined model's training rows, re-filed by term. So the
  crawl-domain training index keeps those queries' known-good pages more reliably than it
  would for unseen queries. Its pools hold fewer hard negatives than a real rewrite would
  produce.
- **The pools differ in size.** crawl-domain's pools add 14.2k new rows against the heuristic's
  8.2k, because they overlap Pass 2's production pools less.
- **Next:**
  1. Check that the heuristic-pool model doesn't cost anything with Staan in the pool, then
     consider shipping it. Deeper or more index-only pools may help further.
  2. If the write order is revisited: the subdomains-per-registered-domain and language
     features from #463's list, and value per byte. Then rerun this matched comparison, which
     is the fair test.
