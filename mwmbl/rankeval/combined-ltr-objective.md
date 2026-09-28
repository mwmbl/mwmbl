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
- **Domain-quality features don't help end to end.** The crawl model's domain features
  (curated, Google SERP host counts, host shape, crawl inlinks) gain +0.006 to +0.008 in
  cross-validation and stack with MiniLM there. On en-gb they add +0.001 [−0.004, +0.006],
  and nothing beside `both`. A learned host-quality model does no better. Keep MiniLM.
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
- **Not yet measured:** the serving cost of scoring every candidate the LTR keeps.

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

To reproduce:

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/domain_features.py   # crawl host table
PYTHONPATH=. uv run python scripts/combined_ltr_labels/domain_experiment.py [--minilm]
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_domain_eval.py report
```

`serp_domains.json` and `curated_domains.json` come from `crawl_model.py serp` on the
crawl-page-model branch. They, and the raw crawl, live in `devdata/index_write_order/`
and are not committed.

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
4. **MiniLM feature.** To add `both`, score every candidate the ranker keeps with the
   Super Search judge before the LTR, and measure what that costs in latency: about 1/76 s
   a candidate on CPU.

To reproduce:

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/objective_experiment.py
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_eval.py report
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py report
```
