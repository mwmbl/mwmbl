# Combined Search: provider features + keep-Staan — handover

2026-09-23 · branch `combined-search-eval` (uncommitted work on top of `27fce55`)

**Decision: ship it.** Three changes to Combined Search together beat calling Staan alone on
the blind judge, for the first time:

1. **No Wikipedia fetch.** Pool the index + Staan only.
2. **Keep Staan's results.** Exempt them from the model's majority-terms filter, which zeroes
   (and so drops) any candidate that matches half the query terms or fewer.
3. **Staan's rank as model features.** `in_staan` and `staan_rank` let the model learn how far
   to trust Staan's order.

Together (arm `provider-nowiki-keep`):

- **Judge NDCG@10:** 0.765, against Staan's 0.750 (+0.015 [+0.005, +0.026]).
- **Gold NDCG:** 0.664, against Staan's 0.734. Still behind, but the gap is half what it was
  (−0.070, down from −0.138).

Brave is still ahead on both measures (0.757 gold, 0.859 judge).

The previous handover, with the method and the Brave and latency findings, is at
https://claude.ai/code/artifact/9931df9a-ebd1-4f18-b751-a23b48904fc9.

---

## Results (absolute)

These are 298 queries: 5% of the gold test set, with the same seed-42 sample as the previous
handover. None of the 298 is among the 849 training queries (overlap checked: 0).

The index side is production (`RemoteIndex`), and every combined-family arm uses MMR.

- **Gold NDCG:** agreement with scraped Google results.
- **Judge NDCG@10:** each list scored against the ideal ordering of the pooled top 10s, using
  the local MiniLM cross-encoder (`minilm-both-v1`), which never sees Google.
- **Judge mean:** the mean judge score of the returned results.

| Arm | Gold NDCG | Gold recall | Judge NDCG@10 | Judge mean | Results returned |
|---|---|---|---|---|---|
| brave | **0.757** ± 0.016 | 33.1% | **0.859** ± 0.005 | 0.643 | 10.00 |
| staan | 0.734 ± 0.018 | 28.5% | 0.750 ± 0.008 | 0.611 | 8.80 |
| **provider-nowiki-keep** (ship this) | 0.664 ± 0.019 | 25.2% | **0.765** ± 0.007 | 0.560 | 9.87 |
| provider-nowiki | 0.645 ± 0.021 | 21.1% | 0.707 ± 0.010 | 0.540 | 9.12 |
| combined-keep | 0.610 ± 0.018 | 24.5% | 0.749 ± 0.007 | 0.545 | 9.92 |
| combined (current endpoint) | 0.596 ± 0.019 | 20.8% | 0.692 ± 0.010 | 0.520 | 9.29 |
| combined-nowiki-keep | 0.596 ± 0.018 | 24.4% | 0.753 ± 0.006 | 0.553 | 9.87 |
| staan+wiki | 0.594 ± 0.019 | 22.0% | 0.651 ± 0.012 | 0.524 | 8.27 |
| control-nowiki-keep | 0.592 ± 0.017 | 24.7% | 0.753 ± 0.007 | 0.553 | 9.87 |
| combined-nowiki | 0.582 ± 0.019 | 20.7% | 0.694 ± 0.010 | 0.534 | 9.12 |
| mwmbl | 0.348 ± 0.027 | 4.2% | 0.388 ± 0.013 | 0.302 | 8.10 |

± is the standard error of the mean. For the judge mean, and for gold recall, the SEM is 0.011
or less.

Judge NDCG is scored against the pool of every arm's top 10, and this run has more arms than
the last one. Numbers for the old arms therefore shift slightly: `combined` minus `staan` on
the judge was −0.059 last time and is −0.058 now.

### What each arm is

| Arm | Pool | Model | Term filter on Staan results |
|---|---|---|---|
| staan | Staan's top 10, Staan's order | — | — |
| brave | Brave API top 10 | — | — |
| combined | index + Staan + Wikipedia | shipped `model-combined.xgb` | applied |
| combined-nowiki | index + Staan | shipped | applied |
| combined-keep | index + Staan + Wikipedia | shipped | exempt |
| combined-nowiki-keep | index + Staan | shipped | exempt |
| control-nowiki-keep | index + Staan | Python retrain, 50 features | exempt |
| provider-nowiki | index + Staan | Python retrain, 52 features | applied |
| provider-nowiki-keep | index + Staan | Python retrain, 52 features | exempt |
| staan+wiki | Staan + Wikipedia over an empty index | shipped | applied |
| mwmbl | index + Wikipedia | shipped | applied |

### Paired differences (A − B), bootstrap 95% CI, judge wins/ties/losses

| Comparison | Gold NDCG | Judge NDCG@10 | Judge W/T/L |
|---|---|---|---|
| **provider-nowiki-keep − staan** | −0.070 [−0.096, −0.045] | **+0.015 [+0.005, +0.026]** | 172/0/126 |
| provider-nowiki-keep − combined | +0.068 [+0.039, +0.098] | +0.073 [+0.058, +0.090] | 220/0/78 |
| *Each change on its own* | | | |
| combined-nowiki − combined (1. no wiki) | −0.014 [−0.028, −0.002] | +0.003 [−0.001, +0.006] | 94/106/98 |
| combined-keep − combined (2. keep Staan) | +0.014 [−0.003, +0.033] | +0.057 [+0.043, +0.072] | 114/163/21 |
| provider-nowiki-keep − control-nowiki-keep (3. provider features) | +0.072 [+0.047, +0.097] | +0.013 [+0.007, +0.019] | 171/1/126 |
| control-nowiki-keep − combined-nowiki-keep (retrain check) | −0.004 [−0.022, +0.015] | −0.000 [−0.004, +0.004] | 152/2/144 |
| *Against Staan* | | | |
| combined − staan | −0.138 [−0.170, −0.107] | −0.058 [−0.078, −0.040] | 113/0/185 |
| combined-keep − staan | −0.124 [−0.150, −0.099] | −0.002 [−0.013, +0.009] | 148/0/150 |
| combined-nowiki-keep − staan | −0.139 [−0.164, −0.115] | +0.002 [−0.009, +0.013] | 154/0/144 |
| provider-nowiki − staan | −0.089 [−0.122, −0.058] | −0.043 [−0.063, −0.024] | 139/0/159 |
| *Brave* | | | |
| combined − brave | −0.161 [−0.197, −0.127] | −0.167 [−0.188, −0.147] | 31/0/267 |
| staan − brave | −0.023 [−0.052, +0.005] | −0.109 [−0.124, −0.094] | 47/0/251 |

### Reading it

- **Keeping Staan's results is the judge win.** The term filter zeroes about 38% of candidate
  rows in the LLM dataset. With Staan's results exempt, Combined returns 9.9 results rather
  than 9.3, and it goes from −0.058 against Staan on the judge to level.
- **Provider features are the gold win.** Gold rewards looking like Google, and Staan's order
  looks like Google, so a model that can see Staan's rank moves towards it. The control
  retrain matches the shipped model on both measures, so the gain comes from the features, not
  from the retrain.
- **Dropping Wikipedia is roughly neutral.** Gold falls 0.014 (significant) and the judge
  doesn't move. It saves a fetch, but it isn't a quality win. The provider + keep model *with*
  Wikipedia was not run; see "Open questions".
- **Mwmbl's own results inside `combined`** are unchanged from the last handover. They take
  2.6 of the top 10 on average (82% of queries have at least one) and judge 0.34, against 0.28
  for the Staan and Wikipedia results they displace.

### Latency (from the previous handover, unchanged)

These are 100 uncached calls, in seconds. Combined's critical path is Staan, and dropping
Wikipedia doesn't change it: that fetch was concurrent.

| Provider | p50 | p90 | p99 | Mean |
|---|---|---|---|---|
| brave | 1.00 | 1.25 | 1.63 | 1.00 |
| staan (≈ combined) | 1.36 | 1.97 | 4.32 | 1.53 |

---

## What was built (uncommitted)

- **`mwmbl_rank/src/lib.rs`**: new static method `RustXGBPipeline.extract_features(records)`.
  It returns the 50-column feature matrix that `predict` scores, one row per record.
  - Python XGBoost loading `model-combined.xgb` on this matrix reproduces Rust's `predict`
    exactly before the filter (max difference 0.0 over 2,000 rows).
- **`mwmbl/rankeval/ltr/provider_features.py`** (new): trains the combined model's exact config
  as a **Python** XGBoost booster over the Rust features, plus `in_staan` (0/1) and
  `staan_rank` (0-based; NaN when not in Staan).
  - The config is mixed: ext weight 0.25, overall ≥ 4, `scale_pos_weight` 1.0, `reg_lambda`
    2.0, 100 rounds, exact tree method, curation weight 0.5, weak negatives 0.25.
  - Both features are keyed on the URL, not the document's source, so an index copy of a page
    Staan also returned carries them too.
  - Training Staan ranks come from `devdata/llm_relabel/pass2_staan.jsonl` (all 849 LLM
    queries), so there are no API calls. 62,638 of 330,090 rows have Staan asked, and 7,491
    are in Staan's results; the second number exactly matches the dataset's `staan` pool.
  - Rows whose query Staan was never asked (most extension and curation rows) get NaN for
    **both** features. Serving must follow the same convention: NaN means "Staan wasn't
    asked", 0/NaN means "not in Staan's results".
  - `--no-provider-features` trains the 50-feature control.
  - Artifacts: `devdata/rankeval-2026-04/model-combined-provider.json` (ship this) and
    `model-combined-control.json`.
- **`mwmbl/rankeval/evaluation/compare_combined_providers.py`**: six new arms and a
  `BoosterRanker` (an `LTRRanker` subclass scored by a Python booster).
  - It has switches for provider features and the keep-Staan exemption. The no-Wikipedia arms
    just leave Wikipedia out of the pool.
  - In the ranker, Staan's rank is recovered from the Staan result's own score
    (`STAAN_TOP_SCORE − score`), so the blacklist filter can't shift it.
- **Outputs:** the full log is `devdata/combined_providers_eval/run-experiments.log`, and the
  per-query rows are `rows-0.05.json`. The previous run's rows are kept as
  `rows-0.05-handover.json`.

---

## Shipping plan

The eval ranker is the reference implementation: `BoosterRanker` in
`compare_combined_providers.py`. Everything below should reproduce `provider-nowiki-keep`
exactly.

### 1. Choose how to serve the 52-feature model

- **Option A (recommended, fastest): serve the Python booster over the Rust features.**
  `xgboost-cpu` is already a runtime dependency (`pyproject.toml`), and
  `extract_features` does the expensive part in Rust. Add a combined-only ranker, essentially
  `BoosterRanker` moved into `mwmbl/tinysearchengine/`, and point `search_setup.py`'s
  `combined_ranker` at it.
  - Copy `model-combined-provider.json` to `mwmbl/resources/` and add a settings path next to
    `COMBINED_MODEL_PATH`.
  - The training side then stays in `provider_features.py`.
- **Option B (cleaner long-term): port the two features into the Rust pipeline.**
  `DocumentRecord` would need optional `staan_rank` / `in_staan` fields, read from the record
  dict. `NUM_FEATURES` is shared with standard search's 50-feature `model.xgb`, so the extra
  columns must be opt-in per pipeline (for example, a flag on `XGBPipeline`, or inferred from
  the booster's feature count).
  - `predict` also needs a per-record filter exemption.
  - Retrain in Rust, then re-run the eval: the Rust and Python trainings aren't bit-identical.
    The shipped Rust model and the Python control agree at Spearman 0.98 on predictions and
    are level on both eval measures.

**Don't change `LTRRanker.order_results` or Rust `predict` for standard search.** The filter
exemption and the extra features belong to the combined path only.

### 2. Endpoint changes (`mwmbl/tinysearchengine/combined_search.py`)

- **Drop the Wikipedia fetch.** `_gather_external` should call `get_staan_results` only.
  - Update the module docstring and `DESCRIPTION`: "three sources" becomes the index + Staan.
  - Keep `wikipedia` in the engine list. Index pages in state `FROM_WIKI` or
    `FROM_WIKI_APPROVED` are still labelled `wikipedia` (`mwmbl/format.py`), so the label can
    still appear.
  - `test/test_combined_search.py` stubs a `WIKI_RESULT` and asserts that each source reaches
    the pool, so it needs updating.
- **Staan failure.** `get_staan_results` returns `[]` when Staan is down, times out or isn't
  configured. That's indistinguishable from "Staan has no results", but the provider features
  should be **NaN** (never asked) for the first and **0/NaN** (asked, not in the results) for
  the second.
  - The eval never hit a failure, so this path is untested.
  - Either have the endpoint distinguish the two, for example through
    `get_cached_external_results(...) is not None` as the Pass-2 script does, or accept the
    small mismatch and document it.
  - With no Staan results, the pool is index-only, so it matters less than it sounds.
- **Keep-Staan exemption.** In the eval, a result is exempt when
  `page.source == DocumentSource.STAAN`. Index copies of the same URL are *not* exempt; they
  still get `in_staan=1`. `Ranker.search` dedups by URL after ordering, keeping the first.

### 3. Tests

- A unit test for the combined ranker:
  - A Staan result that fails the term filter is kept.
  - An index result that fails it is still dropped.
  - `staan_rank` is derived from the score.
  - Features are NaN when there are no Staan candidates (decide what the right behaviour is
    first; see above).
- The endpoint test, updated for no Wikipedia.
- A Rust test for `extract_features` if it stays: shape `n × NUM_FEATURES` and equality with
  `extract_features_batch`.
- Run: `DATABASE_URL="postgres://daoud@" uv run --no-sync pytest test/test_combined_search.py`,
  then `make check`.

### 4. Re-verify after the port

Re-run the eval with the shipped ranker as an arm, or just point `combined` at it. It should
land on the `provider-nowiki-keep` numbers above: 0.664 gold, 0.765 judge.

### 5. Docs

- `mwmbl/rankeval/README.md`: add the provider-features training command next to the
  `llm_experiment` steps, and amend the "value is recall" claim. Combined now beats Staan on
  the judge; it trails on gold.

---

## Open questions and caveats

- **Provider + keep *with* Wikipedia wasn't run.** Dropping Wikipedia cost 0.014 gold on its
  own with the old model. With the new model it might cost nothing, or help. Running it is
  cheap (everything is cached): add
  `booster_arm(provider_booster, True, keep_staan=True, with_wiki=True)`.
- **The provider model was trained with Wikipedia in the pool.** The training pool (`standard`)
  included Wikipedia candidates, but the serving pool won't. The eval says this is fine, but
  it's a train/serve difference.
- **The judge is MiniLM, not people.** Its correlation with curators is 0.40 on held-out
  sources. The +0.015 over Staan is significant but small. A Haiku Pass-3 pass
  (`scripts/llm_relabel_pass3_judge.py`) over the pooled top 10s of `staan` against
  `provider-nowiki-keep` would firm it up.
- **Gold still favours Staan by 0.070.** A `staan-first` arm (Staan's order kept, Mwmbl
  results inserted) is still untested and might close it.
- **Latency is unchanged.** Brave is faster; see the table above. A Staan timeout of about 1.5s
  (currently `STAAN_TIMEOUT_SECONDS = 5`) is the lever.

---

## How to reproduce

```sh
# The Rust extension must include extract_features. `uv run` re-syncs and silently
# reinstalls the OLD build, so after rebuilding use .venv/bin/python or `uv run --no-sync`.
.venv/bin/maturin develop --release && make patch-xgboost

# Train both models (~a few minutes each; no API calls)
DATABASE_URL="postgres://daoud@" .venv/bin/python -m mwmbl.rankeval.ltr.provider_features \
    --no-provider-features --save-model devdata/rankeval-2026-04/model-combined-control.json
DATABASE_URL="postgres://daoud@" .venv/bin/python -m mwmbl.rankeval.ltr.provider_features \
    --save-model devdata/rankeval-2026-04/model-combined-provider.json

# Eval, all arms. .env is not auto-loaded; without it, Staan silently returns nothing.
set -a && source .env && set +a
DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    .venv/bin/python -m mwmbl.rankeval.evaluation.compare_combined_providers --fraction 0.05
```

Staan results are in the external cache with a 1-week TTL. Past that, a re-run pays for
(and may slightly change) Staan's results.
