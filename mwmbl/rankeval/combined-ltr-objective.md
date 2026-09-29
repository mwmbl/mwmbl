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
- **The MiniLM judges add a little more, end to end.** With the served judge (`both`) as a
  feature, `ndcg+new` gains +0.005 [+0.000, +0.011] on en-gb, +0.018 over shipped in all.
  That agrees with cross-validation (+0.006), and `both` is as good as any other judge.
  - **But scoring every candidate costs about a second a query.** The ranker scores 138
    candidates a query on average. The judge takes 1.0 s mean and 1.7 s p90 on 4 threads,
    and 2.5 s mean on 1 thread, against 5 ms for the whole shipped ranking.
- **Common Crawl's web-graph ranks are the domain signal that carries over.** Harmonic
  centrality and PageRank, host and domain level, gain +0.005 on en-gb [+0.000, +0.009],
  matching cross-validation. The crawl model's features (SERP counts, curated, crawl
  inlinks) gain +0.007 in cross-validation, but only +0.001 on en-gb.
  - Beside MiniLM, `both` plus the SERP features plus Common Crawl is the best arm: 0.813,
    +0.008 over `ndcg+new` [+0.003, +0.014] and +0.004 over `both` [−0.001, +0.009].
  - Target encoding of the registered domain's `ethos` alone doesn't overfit, where the
    host-level encoding did, but it isn't significant end to end.
- **The gain is modest against the gap.** The best arm reaches 0.809. Brave scores 0.892,
  and the ordering ceiling is about 0.90. A better objective alone doesn't close it.
- **MMR is why no learned ordering beat Staan-first.** Without MMR, `ndcg+new` gains 0.018
  on en-gb and ties Staan-first. Trained on en-gb Staan results as well, it reaches 0.830,
  +0.004 [−0.001, +0.010] over Staan-first. See the MMR section below.
- **Interleaving index results while keeping Staan's order gains little.** In
  cross-validation, the best arm (the `ndcg+new` model with MiniLM as a feature, letting an
  index result in only when it outscores every remaining Staan result) gains +0.005 over
  Staan-first, half of it from a better fill order. An index-vs-Staan pair classifier does no
  better. An oracle merge would gain +0.07, so choosing the index results is the bottleneck.
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

## MiniLM judges as features (5 folds over the 424 judge-eval queries)

Each fine-tuned MiniLM judge in `devdata/judge_train/models/` adds its score as one feature
to `ndcg+new`: `both` (the served judge), `pointwise` (LLM grades only) and `pairs` (human
curation pairs only). `minilm_scores.py` scores the pairs and `minilm_experiment.py` runs
the arms.

- **Leakage.** The judges were trained on 340 of the 849 queries and checkpoint-selected on
  85 more. So the test folds are drawn only from the other 424, and none of the judges'
  training or validation files, pointwise or pairs, contains any of those 424 queries.
- **Two training sets.** `clean` trains on the other eval folds only. `all` also trains on
  the 425 judge-train/val queries, where the scores are in-sample: `both` has Spearman
  0.71 with `overall` there, against 0.65 on eval queries.
- **Extension rows.** They aren't scored (252k pairs at 76 a second), so their MiniLM
  features are missing.

| Arm | Serving pool NDCG@10 | vs same-training base | Original pool NDCG@10 | vs same-training base |
|---|---|---|---|---|
| clean/base | 0.8699 | — | 0.8193 | — |
| clean/both | 0.8771 | +0.0072 [+0.0025, +0.0119] | 0.8233 | +0.0040 [−0.0012, +0.0094] |
| clean/pointwise | 0.8782 | +0.0083 [+0.0034, +0.0129] | 0.8277 | +0.0084 [+0.0028, +0.0140] |
| clean/pairs | 0.8718 | +0.0019 [−0.0019, +0.0057] | 0.8177 | −0.0016 [−0.0062, +0.0029] |
| clean/all3 | 0.8798 | +0.0099 [+0.0052, +0.0147] | 0.8271 | +0.0079 [+0.0019, +0.0137] |
| all/base | 0.8763 | — | 0.8246 | — |
| all/both | 0.8821 | +0.0058 [+0.0009, +0.0106] | 0.8316 | +0.0070 [+0.0014, +0.0123] |
| all/pointwise | 0.8821 | +0.0058 [+0.0011, +0.0105] | 0.8326 | +0.0080 [+0.0019, +0.0141] |
| all/pairs | 0.8789 | +0.0025 [−0.0007, +0.0059] | 0.8277 | +0.0030 [−0.0011, +0.0068] |
| **all/all3** | **0.8822** | **+0.0058 [+0.0010, +0.0107]** | **0.8325** | **+0.0079 [+0.0021, +0.0135]** |
| MiniLM `both` alone | 0.8246 | −0.0453 vs clean/base | 0.7785 | −0.0408 vs clean/base |

- **The judges help, modestly.** They add +0.006 to +0.010 NDCG@10, about as much again as
  the switch to rank:ndcg gained.
- **The LLM-grade signal carries it.** `pointwise` does as well as `both`, and `pairs`
  alone is not significant. All three together match the best single judge, so one
  feature (`pointwise` or `both`) would do.
- **The in-sample scores don't hurt.** The `all` arms stay best in absolute terms: 425 more
  training queries are worth more than the in-sample optimism costs.
- **MiniLM doesn't replace the LTR.** Ordering by `both` alone is 0.04 worse, and it lets
  far more index results into the top ten.
- **Serving cost:** about a second a query; see the latency section below.

## End to end on en-gb with MiniLM features (289 queries)

`engb_minilm_eval.py` trains `ndcg+new` with each judge set on all 849 queries plus the
serving-pool labels (the `all` setting), and ranks one fresh retrieval as `engb_eval.py`
does. At serving each judge scores every candidate the ranker scores, on its title and
extract. `shipped` and `ndcg+new` rank the same retrieval again, since arms are only
comparable within one. Haiku graded the top-ten URLs no earlier judgment covered: 154 new
judgments and 198 anchors.

| Arm | NDCG@10 | vs shipped | vs ndcg+new | Weak (≤ 3) in top 10 |
|---|---|---|---|---|
| shipped | 0.794 | — | −0.013 [−0.018, −0.008] | 21.0% |
| ndcg+new | 0.807 | +0.013 [+0.008, +0.018] | — | 20.0% |
| ndcg+new+pointwise | 0.814 | +0.020 [+0.013, +0.027] | +0.007 [+0.001, +0.013] | 18.3% |
| **ndcg+new+both** | **0.812** | **+0.018 [+0.012, +0.024]** | **+0.005 [+0.000, +0.011]** | **18.5%** |
| ndcg+new+all3 | 0.812 | +0.019 [+0.012, +0.025] | +0.006 [+0.000, +0.011] | 18.6% |
| brave | 0.891 | +0.097 | +0.084 | 9.8% |

- **The retrieval reproduces the previous run.** `shipped` and `ndcg+new` score 0.794 and
  0.807 here, against 0.796 and 0.809 in the objective run.
- **Use `both`.** It is within 0.002 of `pointwise`, well inside the noise, as in
  cross-validation. It is the judge Super Search already serves
  (`SUPER_SEARCH_JUDGE_MODEL_DIR`), and it was trained on the human curation pairs too.
- **The gain over `ndcg+new` is borderline on its own.** It fits the cross-validation in
  sign and size, and it cuts weak results in the top ten by 1.5 points.
- **Judge offsets.** The anchors run +0.63 generous, but unevenly: +0.92 for the judge of
  the 249-candidate batch, −0.03 for the other. Removing each judge's mean drift from its
  new judgments leaves `both` at +0.017 [+0.011, +0.023] over shipped and +0.004
  [−0.001, +0.010] over `ndcg+new`. Anchor relevance drift is +0.24, 67% of overall grades
  are within one of the original, and Spearman is 0.79.
- **Newly judged URLs** make up 2–3% of the MiniLM arms' top tens, and 0.2% of
  `ndcg+new`'s.
- **Leakage.** Dropping "bitcoin price" and "microsoft teams", which the pairs task may
  have seen, leaves `both` at +0.019 over shipped and +0.006 [+0.000, +0.011] over
  `ndcg+new`.

## Latency of the MiniLM feature (295 en-gb queries)

`minilm_latency.py` replays the en-gb retrieval through the shipped `CombinedLTRRanker`. It
captures the records each query passes to the model: every candidate the served arm's judge
would score. It then times `Judge.score` on them, best of 3 runs, on a laptop i7-8665U
(4 cores, 8 threads).

- **Candidates a query:** 138 on average, median 122, p90 230, max 391. That's many more
  than the ranker returns, because the model scores the whole pool before the `> 0` filter.
- **Pairs are short:** 40 tokens on average, and none reach the 256-token cap, so there's
  little to gain from truncating.

| What is timed | Mean | p50 | p90 | p99 |
|---|---|---|---|---|
| Shipped LTR `predict` | 2 ms | 2 ms | 2 ms | 4 ms |
| Shipped `search_retrieved` (whole ranking + MMR) | 5 ms | 4 ms | 7 ms | 9 ms |
| Judge, all candidates, 1 thread | 2,460 ms | 2,135 ms | 3,983 ms | 7,237 ms |
| Judge, all candidates, 2 threads | 1,514 ms | 1,337 ms | 2,444 ms | 4,451 ms |
| Judge, all candidates, 4 threads | 1,028 ms | 909 ms | 1,681 ms | 3,030 ms |
| Judge, all candidates, onnxruntime default | 1,152 ms | 1,035 ms | 2,017 ms | 3,232 ms |
| Judge, LTR top 10, 1 thread | 162 ms | 157 ms | 188 ms | 232 ms |
| Judge, LTR top 20, 1 thread | 338 ms | 328 ms | 384 ms | 556 ms |
| Judge, LTR top 30, 1 thread | 521 ms | 509 ms | 587 ms | 847 ms |

- **Per candidate:** 17.8 ms on 1 thread (56 a second), 11.0 ms on 2 and 7.5 ms on 4. The
  1/76 s estimate above was optimistic. Threads scale sublinearly, and more threads per
  request would take cores from concurrent requests under load.
- **200–500× the ranking it would feed.** Scoring every candidate turns a 5 ms ranking
  into 1–2.5 s. That is as much again as the whole request: 1.2–1.3 s end to end on beta
  and production, mostly Staan.
- **The judge can't overlap Staan.** It scores the pooled candidates, which include Staan's
  results, so it runs after the fetch. It could score the index candidates while Staan is
  in flight, leaving only Staan's ~10 results after it: about 160–180 ms on 1 thread. But
  the index lookup finishes much sooner than Staan's fetch, and Staan-cache hits leave no
  fetch to hide behind.
- **A two-stage judge is affordable.** Scoring only the LTR's top 10–30 costs 0.16–0.5 s on
  1 thread. The cascade sections below test it: the top 20 keeps the whole gain, in
  cross-validation and end to end.
- **Production hardware isn't measured here.** These are laptop timings. The ratio to the
  LTR should carry over. The absolute numbers depend on the server's cores and on how
  many requests share them.

## Domain-quality features (2026-09-28)

Before adding MiniLM, does a richer domain signal do the same job? The shared features have
one, `domain_score` (HN top-domains rank). `domain_features.py` adds what the write-time
crawl model uses (PR #463, `index-write-order-crawl-model.md`):

- `curated`;
- Google SERP counts: `serp_queries_host`, `serp_top3_host`, `serp_queries_apex`. The table
  leaves out every en-gb query, and each training row's own query;
- host shape, and a TLD code;
- `crawl_pages` and `crawl_inlink_hosts`, from the raw crawl's 458k pages and their links.

`domain_experiment.py` compares these with two learned signals. Both are cross-fitted
within each training set:

- `hq`, a host-quality model: an XGBoost regressor from those columns plus `domain_score`
  to the Pass-3 `ethos` grade;
- `te`, target encoding: the smoothed mean `ethos` and `overall` of the host and of its
  registered domain.

### Cross-validation

The first table uses the objective experiment's 5 folds over 849 queries. The second is
beside MiniLM `both`, over the 424 judge-eval queries in the `all` setting. Deltas are
against each table's `ndcg+new` base, which reproduces 0.8761 / 0.8235.

| Arm | Serving pool Δ | Original pool Δ |
|---|---|---|
| serp | +0.0053 [+0.0025, +0.0079] | +0.0054 [+0.0023, +0.0087] |
| raw (serp + shape) | +0.0066 [+0.0038, +0.0096] | +0.0061 [+0.0028, +0.0094] |
| raw+crawl | +0.0064 [+0.0036, +0.0091] | +0.0080 [+0.0051, +0.0111] |
| hq | +0.0058 [+0.0029, +0.0086] | +0.0042 [+0.0011, +0.0075] |
| te | +0.0035 [+0.0007, +0.0062] | −0.0041 [−0.0080, −0.0004] |
| all (raw+crawl, hq, te) | +0.0050 [+0.0022, +0.0080] | +0.0009 [−0.0031, +0.0051] |

| Arm (424 queries) | Serving pool Δ | Original pool Δ |
|---|---|---|
| both | +0.0058 [+0.0011, +0.0104] | +0.0070 [+0.0015, +0.0128] |
| raw+crawl | +0.0078 [+0.0037, +0.0117] | +0.0053 [+0.0002, +0.0102] |
| hq | +0.0060 [+0.0021, +0.0098] | +0.0035 [−0.0007, +0.0074] |
| both+raw+crawl | +0.0111 [+0.0061, +0.0166] | +0.0117 [+0.0063, +0.0175] |
| both+hq | +0.0090 [+0.0042, +0.0144] | +0.0082 [+0.0026, +0.0139] |
| both+all | +0.0105 [+0.0049, +0.0157] | +0.0127 [+0.0067, +0.0189] |

- In cross-validation, the plain features are as good as the learned ones.
- Target encoding overfits: it memorises hosts.

### End to end on en-gb (289 queries)

`engb_domain_eval.py` follows `engb_minilm_eval.py`. Haiku graded 147 new URLs, with 188
anchors (overall drift −0.21).

| Arm | NDCG@10 | vs ndcg+new | vs ndcg+new+both | Weak (≤ 3) in top 10 |
|---|---|---|---|---|
| shipped | 0.792 | −0.013 [−0.018, −0.008] | −0.018 [−0.024, −0.011] | 21.0% |
| ndcg+new | 0.805 | — | −0.005 [−0.010, +0.001] | 20.1% |
| ndcg+new+domain | 0.806 | +0.001 [−0.004, +0.006] | −0.004 [−0.010, +0.002] | 19.5% |
| ndcg+new+hq | 0.805 | −0.000 [−0.005, +0.005] | −0.005 [−0.011, +0.001] | 19.3% |
| **ndcg+new+both** | **0.810** | **+0.005 [−0.000, +0.010]** | — | **18.7%** |
| ndcg+new+both+domain | 0.810 | +0.005 [−0.001, +0.011] | +0.000 [−0.004, +0.005] | 18.3% |
| brave | 0.889 | +0.084 | +0.079 | 9.8% |

- **The cross-validation gain doesn't carry over.** MiniLM's does: +0.005 here against
  +0.006 in cross-validation.
- **Leakage through similar queries doesn't explain the gap.** A query in the SERP table
  that contains, or is contained in, the row's query can still count. But 53% of the 849
  training queries have such a near-duplicate, and so do 56% of the en-gb queries.
- **The domain features reshuffle reputable hosts.**
  - `+domain` changes the top ten of 170 queries, swapping 272 URLs each way.
  - The URLs it brings in grade only 0.2 higher on average (3.81 against 3.59).
  - The hosts on both sides are the same: Wikipedia, the Guardian, the BBC, YouTube.
- **The weak results are mostly on known hosts.**
  - 65% of the weak results in `ndcg+new`'s top ten are on curated or SERP-listed hosts,
    against 75% of the good ones.
  - What remains is a relevance problem (off-topic pages on good sites), not spam, and
    MiniLM addresses it. The spam these features fixed at write time (#463) mostly never
    reaches this top ten.
- **A likely reason for the cross-validation gain:** the pools are graded with Google SERP
  presence, and the extension rows' labels are Google SERPs. A host's SERP count mostly
  says "Google likes this host", which the training labels reward more than fresh
  retrieval does. This is plausible but untested.

### Common Crawl ranks and a stricter target encoding

The crawl inlinks come from a small, skewed local crawl: 458k pages from about 9.5k source
hosts, counted without weighting. So a second run swaps in Common Crawl's Jul–Sep 2026 web
graph (`cc-main-2026-jul-aug-sep`, 246M hosts and 2.7B edges).

- **`cc`:** harmonic centrality and PageRank positions, host and domain level. Each graph is
  cut to its top 5M by either measure, and anything below that is missing. 96% of the
  serving pool's index hosts are ranked, and 98% of their domains.
- **`te-ethos`:** the registered domain's mean `ethos` alone, smoothed with a prior of 20
  rows, cross-fitted as before.

Cross-validation (Δ against each table's base):

| Arm | Serving pool Δ | Original pool Δ |
|---|---|---|
| raw | +0.0066 [+0.0037, +0.0094] | +0.0061 [+0.0028, +0.0094] |
| cc | +0.0043 [+0.0017, +0.0069] | +0.0048 [+0.0019, +0.0079] |
| raw+cc | +0.0057 [+0.0029, +0.0086] | +0.0070 [+0.0038, +0.0105] |
| te-ethos | +0.0068 [+0.0039, +0.0095] | +0.0030 [+0.0002, +0.0057] |

| Arm (424 queries) | Serving pool Δ | Original pool Δ |
|---|---|---|
| both | +0.0058 [+0.0011, +0.0104] | +0.0070 [+0.0012, +0.0125] |
| both+raw | +0.0090 [+0.0035, +0.0145] | +0.0109 [+0.0051, +0.0169] |
| both+cc | +0.0079 [+0.0026, +0.0133] | +0.0095 [+0.0036, +0.0153] |
| both+raw+cc | +0.0110 [+0.0060, +0.0167] | +0.0129 [+0.0071, +0.0191] |
| both+te-ethos | +0.0087 [+0.0041, +0.0137] | +0.0094 [+0.0042, +0.0152] |

End to end on en-gb (`engb_domain_eval.py --cc`). Haiku graded 130 new URLs, with 162
anchors (overall drift +0.23: +0.14 and +0.68 for the two judges).

| Arm | NDCG@10 | vs ndcg+new | vs ndcg+new+both | Weak (≤ 3) in top 10 |
|---|---|---|---|---|
| shipped | 0.792 | −0.013 [−0.018, −0.008] | −0.017 [−0.024, −0.011] | 21.0% |
| ndcg+new | 0.805 | — | −0.005 [−0.010, +0.001] | 20.1% |
| ndcg+new+raw | 0.806 | +0.002 [−0.003, +0.006] | −0.003 [−0.009, +0.003] | 19.2% |
| ndcg+new+cc | 0.809 | +0.005 [+0.000, +0.009] | −0.000 [−0.006, +0.005] | 19.3% |
| ndcg+new+raw+cc | 0.807 | +0.002 [−0.003, +0.006] | −0.003 [−0.009, +0.003] | 19.4% |
| ndcg+new+te-ethos | 0.808 | +0.003 [−0.000, +0.007] | −0.001 [−0.007, +0.005] | 19.7% |
| ndcg+new+both | 0.810 | +0.005 [−0.000, +0.010] | — | 18.7% |
| **ndcg+new+both+raw+cc** | **0.813** | **+0.008 [+0.003, +0.014]** | **+0.004 [−0.001, +0.009]** | **18.3%** |
| brave | 0.889 | +0.084 | +0.079 | 9.8% |

- **Common Crawl is the one domain signal whose cross-validation gain survives en-gb:**
  +0.005, as MiniLM's does.
- **The SERP features still don't survive on their own.** `raw` gains +0.002. With `cc`
  they add nothing alone, but beside `both` they help.
- **`both+raw+cc` is the best arm in both evaluations.** Its gain over `both` is borderline
  on en-gb (+0.004, CI touching zero) and +0.011 in cross-validation.
- **Robustness:** removing each judge's mean anchor drift from its new judgments leaves
  `both+raw+cc` at +0.008 [+0.003, +0.014] over `ndcg+new` and +0.004 [−0.001, +0.008]
  over `both`. `cc` stays at +0.004 [−0.000, +0.008].
- **Serving `cc`** needs a lookup table: about 8M hosts and 8M domains, which could be cut
  much further, since hosts past the top million or so rarely reach a top ten.
- **Serving `raw`** needs the SERP table and the curated list. The SERP table is a snapshot
  of extension scrapes, so it goes stale, and it covers only the 43.6k hosts seen.

To reproduce:

```sh
# Common Crawl ranks, top 5M by either measure (about 7 GB streamed)
B=https://data.commoncrawl.org/projects/hyperlinkgraph/cc-main-2026-jul-aug-sep
for l in domain host; do
    curl -sS $B/$l/cc-main-2026-jul-aug-sep-$l-ranks.txt.gz | zcat \
        | awk -F'\t' 'NR>1 && ($1<=5000000 || $3<=5000000) {print $1"\t"$2"\t"$3"\t"$4"\t"$5}' \
        > devdata/combined_ltr_labels/cc_${l}_ranks.tsv
done
PYTHONPATH=. uv run python scripts/combined_ltr_labels/domain_features.py   # crawl host table
PYTHONPATH=. uv run python scripts/combined_ltr_labels/domain_experiment.py [--minilm]
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_domain_eval.py report [--cc]
```

`serp_domains.json` and `curated_domains.json` come from `crawl_model.py serp` on the
crawl-page-model branch. They, and the raw crawl, live in `devdata/index_write_order/`
and are not committed.

## MiniLM on the top K only: a cascade (5 folds over the 424 judge-eval queries)

The judge costs about 17 ms a candidate on one thread. That is 2.5 s for all ~150 candidates
of a query, 0.35 s for the LTR's top 20 and 0.52 s for its top 30, against 5 ms for the
shipped ranking. `minilm_cascade_experiment.py` tests a two-stage ranker:

1. `ndcg+new` without MiniLM ranks every candidate.
2. The judge scores that ranking's top K, and a model with `both` as a feature re-orders
   them. Everything below K keeps its stage-1 order.

This isn't the MiniLM re-rank of `combined-search-haiku-eval.md`, which ordered the top K by
the judge's score alone and lost. Here the judge is one feature beside the others.

- **`masked`:** stage 2 is trained with the judge's score present only on its training rows'
  stage-1 top K, as serving would give it. Those rows' stage-1 ranking is out of fold.
- **`full`:** the model trained with the judge on every candidate re-orders the top K.

The setting is `all`, and `base` and `all candidates` reproduce `all/base` and `all/both`.

| Arm | Serving pool NDCG@10 | vs base | vs all candidates | Original pool NDCG@10 | vs base |
|---|---|---|---|---|---|
| base | 0.8763 | — | −0.0058 | 0.8246 | — |
| all candidates | 0.8821 | +0.0058 [+0.0012, +0.0106] | — | 0.8316 | +0.0070 |
| top 10, masked | 0.8799 | +0.0036 [+0.0008, +0.0065] | −0.0022 [−0.0065, +0.0020] | 0.8276 | +0.0030 |
| top 10, full | 0.8805 | +0.0042 | −0.0016 | 0.8292 | +0.0046 |
| top 20, masked | 0.8840 | +0.0076 [+0.0032, +0.0123] | +0.0019 [−0.0012, +0.0049] | 0.8310 | +0.0064 |
| top 20, full | 0.8828 | +0.0064 | +0.0007 | 0.8310 | +0.0064 |
| top 30, masked | 0.8847 | +0.0084 [+0.0038, +0.0129] | +0.0026 [−0.0001, +0.0055] | 0.8334 | +0.0088 |
| top 30, full | 0.8830 | +0.0067 | +0.0009 [+0.0000, +0.0019] | 0.8321 | +0.0074 |

- **Scoring the top 20 or 30 keeps the whole gain.** Every top-20 and top-30 arm is at least
  level with scoring every candidate, and `top 30, masked` is the best arm on both pools.
- **The top 10 keeps about two thirds of it,** at 160 ms.
- **Training on the cascade's own view (`masked`) is as good as reusing the full model or
  better.** Its lead over `all candidates` isn't significant. A plausible reason is that
  stage 1 has already dropped the weak index pages the judge can over-promote.
- **Not yet measured:** the cascade end to end on en-gb.

## The cascade end to end on en-gb, and Staan-first (289 queries, 2026-09-29)

`engb_cascade_eval.py` trains `ndcg+new` and the top-20 cascade (`masked`) on all 849
queries plus the serving-pool labels, each also with Staan-monotone constraints, and ranks
one fresh retrieval. Two arms put Staan's results first in Staan's order and fill the
remaining slots from our candidates: by the cascade's order, or by the judge alone over
`ndcg+new`'s top 30. The `earlier:` arms are the lists of `combined-search-haiku-eval.md`'s
retrieval, scored against the same judgments. Haiku graded 37 new URLs, with 62 anchors
(overall drift −0.11).

**A bug in the earlier en-gb runs.** `engb_eval.staan_documents` took Staan's ranks from
`rows-0.05.json`'s `staan_urls`, which holds Staan's URLs sorted alphabetically. So every en-gb
run above gave the models scrambled Staan ranks: the right results were Staan's, but in the
wrong order. It now reads `lists["staan"]`, Staan's real order. The fix barely moves
`ndcg+new` (0.810 here against 0.807–0.809), so the conclusions above stand.

| Arm | NDCG@10 | vs ndcg+new | vs Staan-first, fill MiniLM | Weak (≤ 3) in top 10 |
|---|---|---|---|---|
| shipped | 0.795 | −0.015 | −0.037 | 21.0% |
| ndcg+new | 0.810 | — | −0.023 | 19.5% |
| ndcg+new, Staan-monotone | 0.809 | −0.001 [−0.004, +0.002] | −0.023 | 19.5% |
| cascade top 20 | 0.816 | +0.006 [+0.001, +0.011] | −0.017 | 18.6% |
| cascade top 20, Staan-monotone | 0.815 | +0.005 [+0.000, +0.010] | −0.018 | 18.8% |
| Staan-first, fill cascade top 20 | 0.829 | +0.020 | −0.003 [−0.005, −0.001] | 16.1% |
| **Staan-first, fill MiniLM(ndcg+new top 30)** | **0.833** | **+0.023 [+0.015, +0.030]** | — | **15.5%** |
| earlier: Staan | 0.794 | −0.016 | −0.039 | 11.2% |
| earlier: Staan-first + fill (shipped) | 0.824 | +0.014 | −0.009 | 17.2% |
| earlier: Staan-first, fill MiniLM(LTR top 30 + Wikipedia) | 0.835 | +0.026 | +0.003 [+0.001, +0.005] | 16.3% |
| brave | 0.889 | +0.079 | +0.056 | 9.8% |

- **The cascade's gain carries over:** +0.006 over `ndcg+new`, the same as cross-validation.
- **Staan-first still beats every learned ordering,** by 0.013–0.017 over the cascade. The
  LTR still lets index pages displace Staan's, even with exact Staan ranks.
- **Filling after Staan, the judge alone beats the cascade** by a small but significant 0.003.
  The fill slots hold our own candidates, mostly index pages, and there the judge orders
  better than the LTR with the judge as a feature.
- **Wikipedia adds a little:** the earlier arm with it is 0.003 above the same fill without it.
  It also ran on a different retrieval, with the shipped LTR's top 30.
- **Staan-monotone constraints do nothing,** here as in cross-validation
  (`minilm_cascade_experiment.py staan-rank|staan`: every arm within ±0.003, and the share of
  the top ten's Staan pairs out of Staan's order stays about 32%).

## Why the LTR couldn't beat Staan-first: MMR (2026-09-29)

Every learned ordering above loses to Staan-first on en-gb. Yet in cross-validation on the
training labels, `ndcg+new` beats Staan-first by 0.008 [+0.004, +0.012], and still does with
near-duplicate queries kept in one fold. So the model can learn Staan's value. Something
between the model and the served list undoes it.

**It is mostly MMR.** Combined Search wraps the LTR in `MMRRanker` (`search_setup.py`).
Cross-validation never applies it, and Staan-first bypasses it.

- MMR's kernel is domain-dominant. A second page from a domain already shown loses
  0.3 × 0.8 = 0.24 of rank-relevance, about 17 places.
- Staan often returns several good pages from one site, such as the Guildford M&S stores.
- On en-gb, 42% of the Staan results that repeat a domain earlier in Staan's list are
  displaced from `ndcg+new`'s top ten (107 of 256), against about 4% of first-of-domain
  ones. The displaced repeats grade 6.2 on average.
- In cross-validation, `mmr_rerank` on the held-out rankings costs 0.023 NDCG@10 on the
  training labels, from 0.879 to 0.856. That puts the LTR 0.016 below Staan-first, which is
  the en-gb picture reproduced from the training data alone.

End to end on en-gb (289 queries, one fresh retrieval; Haiku graded 50 new URLs with 84
anchors, overall drift +0.24):

| Arm | NDCG@10 | vs ndcg+new | vs Staan-first, fill ndcg+new | Weak (≤ 3) in top 10 |
|---|---|---|---|---|
| shipped | 0.795 | −0.013 [−0.019, −0.008] | −0.031 | 20.9% |
| shipped, no MMR | 0.813 | +0.005 [−0.001, +0.011] | −0.013 | 18.3% |
| ndcg+new | 0.808 | — | −0.018 [−0.025, −0.011] | 19.7% |
| ndcg+new, no MMR | 0.826 | +0.018 [+0.014, +0.023] | +0.000 [−0.006, +0.007] | 17.4% |
| ndcg+new (en-gb Staan) | 0.815 | +0.006 [+0.002, +0.010] | −0.012 | 19.4% |
| **ndcg+new (en-gb Staan), no MMR** | **0.830** | **+0.022 [+0.016, +0.028]** | **+0.004 [−0.001, +0.010]** | **17.4%** |
| Staan-first, fill ndcg+new | 0.826 | +0.018 | — | 16.6% |
| Staan-first, fill ndcg+new, no MMR | 0.830 | +0.022 | +0.004 [+0.002, +0.006] | 16.1% |
| brave | 0.888 | +0.080 | +0.062 | 9.8% |

- **Without MMR the learned ordering matches Staan-first.** `ndcg+new` gains 0.018 and ties
  it. The shipped model gains the same 0.018.
- **Training on en-gb Staan adds a little:** +0.006 with MMR and +0.004 without. The best
  learned arm, 0.830, is level with the best Staan-first fill.
- **The MiniLM and Common Crawl gains above were all measured under MMR.** They should be
  re-checked without it, where the headroom is different.
- **NDCG can't see what MMR is for.** Each page is graded alone, so a list of five pages
  from one site isn't penalised as redundant. Removing MMR outright may cost diversity that
  these labels don't measure. A lighter kernel, or exempting Staan's own repeats, would keep
  some of it. Either needs a list-level judgment to evaluate.

### What didn't explain it

- **Staan's market.** The training data's Staan results were fetched at the `en-us`
  default: 6.8% `.uk` hosts, against 30% for en-gb Staan, with US pages in their tail. But
  refetching the 849 queries at `en-gb` (`engb_staan.py`: 7,604 results, 26% `.uk`, 44%
  overlap with en-us) and judging the 3,676 new pairs with the Pass-3 prompt left Staan's
  grade unchanged: 6.31 against 6.30, weak 16.6% against 17.2%.
- **The judging prompt.** `judge_shift.py` re-graded 740 training pairs with the en-gb
  prompt (UK searcher, no intent). Index pages moved +0.24 [−0.13, +0.58] relative to
  Staan's, the opposite of what would explain the gap.

To reproduce:

```sh
set -a; source .env; set +a   # STAAN_SEARCH_API_KEY; about 849 Staan calls
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_staan.py collect   # en-gb settings, own cache
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_staan.py batches   # then Haiku judges -> consolidate
PYTHONPATH=. uv run python scripts/combined_ltr_labels/judge_shift.py batches  # then Haiku judges -> report
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_staan_experiment.py cv
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_staan_experiment.py rank   # then batches / consolidate / report
```

## Interleaving index results into Staan's order (2026-09-29, cross-validation only)

Staan-first wins the holistic comparison against every learned ordering, so these arms keep
its property: Staan's results always appear in Staan's order, and the index's in the model's
order. They differ only in where index results are let in (`interleave_experiment.py`).

- `merge-rest d`: the next index result goes in when it outscores every Staan result still
  to come by more than d. A plain two-list merge against the next Staan result only, fixed
  slots, protecting Staan's top k and capping insertions all did no better.
- `pair t`: a classifier on (index result, Staan result) pairs, the two feature rows side
  by side, trained on the serving pool with grade-gap weights. The next index result goes in
  when it beats the next Staan result with probability above t.
- `oracle merge`: the order-preserving merge that maximises DCG on the true grades.

Every parameter is picked by nested cross-validation on the other four folds.

**Without MiniLM** (5 folds over the 848 en-gb serving-pool queries, `ndcg+new` trained on
en-gb Staan):

| Arm | NDCG@10 | vs Staan-first, 95% CI | Index results in top 10 |
|---|---|---|---|
| Staan-first | 0.8718 | — | 0.92 |
| **merge-rest (tuned: 0.5)** | **0.8733** | **+0.0016 [+0.0006, +0.0026]** | 1.01 |
| merge (tuned) | 0.8725 | +0.0007 [−0.0005, +0.0019] | ~1.1 |
| slot (tuned: 10) | 0.8669 | −0.0049 [−0.0063, −0.0035] | 1.30 |
| DCG-optimal merge on a regression model's grades (tuned) | 0.8705 | −0.0013 [−0.0033, +0.0006] | ~1.0 |
| learned (reorders Staan) | 0.8789 | +0.0071 [+0.0040, +0.0104] | 1.38 |
| oracle merge | 0.9483 | +0.0765 [+0.0704, +0.0826] | 2.91 |

**With MiniLM** (5 folds over the 424 queries no judge saw, as `minilm_experiment.py`;
`minilm-both-v1` as an `ndcg+new` feature, and in the pair classifier). Staan-first scores
0.8771 here.

| Arm (tuned) | clean | all |
|---|---|---|
| merge-rest, no MiniLM | +0.0003 | +0.0014 |
| pair, no MiniLM | +0.0007 | +0.0000 |
| **merge-rest, MiniLM feature** | +0.0036 [+0.0007, +0.0065] | **+0.0046 [+0.0016, +0.0079]** |
| pair, MiniLM feature | +0.0035 [+0.0011, +0.0061] | +0.0045 [+0.0018, +0.0070] |
| merge on the MiniLM score alone | — | +0.0009 [−0.0011, +0.0032] |
| learned (reorders Staan) | +0.0065 | +0.0135 [+0.0080, +0.0185] |
| oracle merge | +0.071 | +0.071 |

- **The headroom is large, but the model can't reach it.** The oracle merge gains +0.071 to
  +0.077: +0.017 from ordering the fill, the rest from placing index results among Staan's.
  The best index result in a query's pool grades 6.3 on average, above Staan's #10 (5.85),
  but the model's top index pick grades 4.9.
- **Loose merges lose.** Any setting that lets in more than about 0.1 extra index results a
  query brings in weak pages. Staan returns fewer than ten results for 56% of queries, so
  the fill already adds 0.9 index results a query.
- **MiniLM as a feature is what helps.** It roughly triples the interleaving gain. Half of
  its gain is the fill order: Staan-first filled by the MiniLM model gets +0.0026 [+0.0003,
  +0.0048]. The insertions add +0.0028 [+0.0009, +0.0048] on top (merge-rest 0.25, untuned).
- **The pair classifier learns nothing the ranking model didn't.** It tunes to p > 0.9,
  where it inserts 0.12 extra index results a query, grading 6.5, about Staan's tail.
- **The `all` setting beats `clean` everywhere,** so the judge's in-sample scores on its
  training queries don't mislead these models.
- These gains, +0.005 at best, are too small for the holistic judge to see without many
  queries. At serving, the judge would only need Staan's ~10 results and the index's top few.

To reproduce (`minilm-engb` adds the 3,676 en-gb Staan pairs to `minilm_scores.json`):

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/interleave_experiment.py oof       # then report
PYTHONPATH=. uv run python scripts/combined_ltr_labels/interleave_experiment.py minilm-engb
PYTHONPATH=. uv run python scripts/combined_ltr_labels/interleave_experiment.py judge-oof  # then judge-report
```

## Caveats

- **The human gate hasn't run.** The curation export (`devdata/judgments_export/`) wasn't
  available, so no arm trains on curation, and the second retrain gate (pair accuracy on
  held-out human curation pairs) wasn't run. Run it before shipping.
- **Approximate Staan ranks.** Where another pool had already added a URL, its Staan rank
  in the LLM training rows is reconstructed, since `pass2_staan.jsonl` wasn't available
  (`objective_experiment.staan_ranks`). `pass2_staan.jsonl` has since turned up locally, so
  exact ranks are now available. The en-gb Staan ranks are exact from the cascade run
  on; the runs before it had them scrambled (see that section).
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
4. **MiniLM.** The best measured ordering is Staan-first, then our other candidates in the
   judge's order over `ndcg+new`'s top 30 (0.52 s on one thread). Without Staan first, a
   top-20 cascade keeps the whole gain of the judge as a feature at 0.35 s.

To reproduce:

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/objective_experiment.py
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_eval.py report
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py report
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_latency.py capture  # then: time
```
