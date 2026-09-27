# A write-time "crawl" model for ordering index pages

2026-09-27 · follows `index-write-order.md` (same directory, PR #456). The NDCG, recovery and
page tables come from `scripts/index_write_order/evaluate.py report`, which reads only
committed data. The attribution and page tables come from `evaluate.py attribution` and
`evaluate.py pages`, which need the local indexes. The offline gate comes from
`crawl_model.py train`.

**Question:** #456 found that ordering a term's index page with the query-time LTR model
fills pages with keyword-stuffed spam. That model was trained on pools the heuristic had
already filtered, so it had never seen that spam. Does a model trained for write time fix
this? Its negatives are every crawled page filed under the term, and it can use domain
information.

The model only orders pages when they are written. Query time is unchanged: every arm here
is ranked by the shipped Combined Search model (`model-combined.xgb`) plus MMR, as in #456.

## Summary

- **The crawl model is much better at the write-time task offline.** On held-out terms, its
  AUC for good pages against crawled pages is 0.958. The heuristic's is 0.625, and the
  query-time model's, given the term, is 0.677.
- **End to end, it still loses to the heuristic, but by less than LTR did:**
  - Haiku NDCG@10 is 0.328 against 0.346, a difference of −0.019 [−0.032, −0.006].
  - #456's `ltr` arm lost by −0.030.
- **The domain features fix the spam.** Top-ten results from numbered spam-blog subdomains:

  | heuristic | crawl-domain | ltr | crawl (no domain features) |
  |---|---|---|---|
  | 32 | 29 | 152 | 130 |

- **It wins at write time.** Where one arm has a good result (Haiku grade ≥2) in its top ten
  and the heuristic does not, or the reverse:
  - the heuristic's index had evicted 256 of crawl-domain's;
  - crawl-domain's index had evicted only 126 of the heuristic's.

  Evicted targets reaching the top ten rise from 16.1% to 20.0%.
- **It loses at query time.** crawl-domain's index keeps 166 of the heuristic's good results,
  but the unchanged query-time model ranks other documents above them. Those documents are
  mostly:
  - off-topic partial matches;
  - non-English Wikipedia editions;
  - spam on blog platforms under letter-named subdomains, such as
    `gregoryqkapd.tokka-blog.com`.

  The query-time model was trained only on pools from the heuristic's index, so it has not
  learned to demote documents that index never held.

**Interpretation and recommendation:** the write-time order is better, and the query-time
model is now the bottleneck. This is a hypothesis, not yet tested: after a rewrite of the
index with this order, the query-time model would need retraining on pools drawn from the
new index before the change is a net win. Until then, keep `INDEX_PAGE_RANKER` at
`heuristic`.

#456's `ltr` arm also wins at write time by this measure (209 against 124), so that alone does
not single the crawl model out. What does is the margin, the spam, and the offline gate.

## Method

- **Positives:** the combined model's training rows, unchanged:
  - Haiku grade ≥4, weight 1;
  - Google SERP presence, weight 0.25;
  - curation, weight 0.5.

  Each row is re-filed as one (term, document) row per lookup term of its query that
  `tokenize_document` files the document under. The pool rows graded below 4 stay as
  negatives.
- **Crawl negatives.** Results in the bucket cannot be used: a crawler indexes its batches
  locally with the heuristic and submits only what survives. So `run_crawl.py` runs the
  crawler's batch workers with submission off and saves every crawled batch before anything
  ranks it.
  - The negatives come from 3,029 batches that were waiting, unindexed, in the local Redis
    queue (May–June 2026; they were exported, not consumed): 165k unique documents.
  - Up to 200 are sampled per training term, which covers 7,031 of 20,254 terms.
  - In all, 876k rows: 64k positive and 338k crawled negatives.
- **Features:** the 50 Rust features with the term as the query and `score` zeroed (a
  document being written has no retrieval score), plus these domain features:
  - `curated`: the host is a moderator-approved domain (6,060 domains, from the public
    endpoint).
  - `serp_queries_host`, `serp_top3_host`, `serp_queries_apex`: how many Google SERPs in the
    Firefox extension's scrapes the host appears in (in the top three, or its registered
    domain appears in). This uses every period, 45.7k files and 14.7k queries. The 295 eval
    queries are left out entirely, and each training row's own query is left out of its
    counts.
  - host shape: labels, digits, hyphens, first-label length, a numbered first label, `www`.
- **Model:** XGBoost `binary:logistic`, 200 rounds. It is trained on 80% of terms (by hash) and
  gated on the rest.
- **A/B:** #456's corpus and harness. That is 6.6M documents: the 2023–24 crawl batches plus
  the live index's pages for the lookup terms of 295 en-gb queries. The corpus is written once
  per arm, then ranked by the shipped query-time model plus MMR and judged blind by Claude
  Haiku 4.5 with the UK relevance prompt. Only the 830 URLs the new arms surfaced needed new
  grades. The corpus shares no crawl with the training negatives.

## Results

Offline gate: mean per-term AUC, 1,149 held-out terms.

| Scorer | vs crawled pages | vs pool negatives |
|---|---|---|
| heuristic | 0.625 | 0.388 |
| combined model, term as query (#456's `ltr`) | 0.677 | 0.796 |
| crawl model | 0.944 | 0.812 |
| crawl model + domain features | 0.958 | 0.850 |

End to end, 269 queries:

| Write-time order | Haiku NDCG@10 | vs heuristic | Good results per query |
|---|---|---|---|
| heuristic | 0.346 | | 3.74 |
| ltr | 0.317 | −0.030 [−0.044, −0.016] | 3.46 |
| crawl | 0.324 | −0.023 [−0.035, −0.010] | 3.59 |
| crawl-domain | 0.328 | −0.019 [−0.032, −0.006] | 3.65 |

Brave's results graded ≥2 that the arms retrieve (gain-weighted):

| Set | Arm | In candidates | In top ten |
|---|---|---|---|
| evicted targets (465) | heuristic | 25.9% | 16.1% |
| evicted targets (465) | crawl-domain | 39.1% | 20.0% |
| controls, in production's top ten (101) | heuristic | 100.0% | 80.6% |
| controls, in production's top ten (101) | crawl-domain | 91.4% | 64.7% |

Where good results went missing:

| Good results in the top ten of | missing from the top ten of | evicted there | kept there, outranked |
|---|---|---|---|
| heuristic | ltr | 124 | 180 |
| ltr | heuristic | 209 | 18 |
| heuristic | crawl | 134 | 150 |
| crawl | heuristic | 234 | 10 |
| heuristic | crawl-domain | 126 | 166 |
| crawl-domain | heuristic | 256 | 11 |

What the lookup terms' pages hold:

| Arm | Documents per term, median | HN-listed share | Repeated hosts per term | Empty extracts |
|---|---|---|---|---|
| heuristic | 44 | 86% | 18.4 | 39% |
| ltr | 29 | 15% | 9.4 | 31% |
| crawl | 27 | 25% | 6.9 | 28% |
| crawl-domain | 28 | 24% | 7.6 | 28% |

## Caveats and next steps

- **Today's crawl is clean.** Only 19 of the 338k negatives (0.006%) come from numbered
  spam-blog hosts, against 5.6% of the A/B corpus. The crawl samples ~6k hosts, weighted to
  HN-listed and curated domains. So the negatives teach the model nothing about spam, and
  only the domain features stop it. The spam that gets through has letter-named subdomains
  on blog platforms: a count of subdomains per registered domain would catch it.
- **Pages hold a third fewer documents.** The median is 28 against 44, because the heuristic's
  URL-length penalty packs pages with short URLs. A page is 4,096 bytes, so ordering by
  value per byte is worth trying.
- **The corpus is mostly 2023–24 text.** A rerun on a fresh crawl needs a few days of
  `run_crawl.py` at about 600k pages a day. `INDEX_WRITE_ORDER_RUN=fresh` sets the harness up
  for it.
- **The domain features are computed in Python,** so building an arm took 90 minutes. They
  would move into `mwmbl_rank` if the model ships.
- **Next:**
  1. Rebuild the eval index with the crawl-domain order.
  2. Retrain the query-time model on pools drawn from it: the LLM-relabel pools re-retrieved
     from the new index, then Haiku-judged.
  3. Rerun this A/B with the retrained query-time model on both arms.
