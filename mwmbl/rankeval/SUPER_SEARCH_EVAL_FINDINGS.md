# Super Search v2 evaluation — findings & handoff

## Goal
Evaluate Super Search v2 vs standard search on a current dataset, starting with the
cosine baseline. This grew into: rebuilding the eval dataset, fixing eval infra,
retraining the LTR model, and a deep investigation of why cosine source selection
underperforms.

Branch: `super-search-2` (all work below committed & pushed).

## Headline findings
1. **Standard search vs Super Search v2 (cosine), 298 test queries, same index:**
   NDCG **0.319 vs 0.293** — Super Search does *not* beat standard search and is
   ~11× slower. (Both use the same local mwmbl index + retrained LTR + MMR + wiki;
   the comparison isolates what Super Search's extra sources/crawl add.)
2. **Why: cosine source selection ≈ random.** Offline source-selection eval (500
   queries × 132 sources): coverage@10 — random 0.44, **cosine 0.45**, popularity
   0.58, **XGBoost (learned) 0.64**, oracle 1.0. Online captured reward — cosine
   0.43, **Thompson-sampling bandit (nu=0.25) 0.80**, oracle 0.99.
3. **A learned/coverage-based policy wins big over cosine** (≈+86% captured reward).
   `popularity` is the dominant feature; cosine features are useless-to-harmful.
4. **But the bandit is contextual-in-form, ~static-in-practice:** a fixed "best-10
   sources every query" list already captures 0.769 vs the bandit's 0.802 — context
   adds only +0.03, because the only predictive features are source-static. Reward is
   concentrated (top-10 generalist sources = 77.6% of all reward).
5. **Three query×source "topical match" features all fail** (corr ≈ 0, too sparse):
   cosine, query↔domain-name overlap (fires 0.1% of cells), mwmbl-index domain hits
   (local & remote, <1%). Source contribution is coverage-driven, not topical. The
   high-reward generalists never lexically match the query. (Feature B is precise when
   it fires — 4× reward on 13 hits — so it could be a cheap *rule*: query contains a
   source's domain token ⇒ force-include that source.)
6. **Wikipedia in eval was silently failing** (HTTP 429 under burst) — fixed with a
   retry adapter; ~36% of queries had been dropping their wiki results.
7. **LTR retrain on the new dataset beats the old model** end-to-end: test NDCG
   0.206 → 0.253 (~+23%). New model committed as production `model.xgb`.

## Commits this session (origin/main..HEAD on `super-search-2`)
- `7eb13db` — Backblaze dataset puller in `extension_dataset.py` (+`--no-download`),
  removed dead Bing pipeline, added `mwmbl/rankeval/README.md`; regenerated gold CSVs
  from full extension-scrape history (2025-07-15→2026-06-24, 7297 train/5969 test q).
- `1b11fbe` — `evaluate_super_search.py`: Super Search pipeline as a `RankingModel`.
- `f51dc41` — cache the Super Search doc-pool (joblib, `devdata/super-search-eval-cache`)
  so ranking iterations re-run free; + `compare_ltr_models.py`.
- `c358ac2` — retrained Rust XGB model + regenerated `learning-to-rank.csv.gz`
  (previous model backed up at `devdata/rankeval-2026-04/model-current.xgb`).
- `397797f` — Wikipedia search API retry on 429 (`rank.py`, `WIKI_RETRY`).
- `8c1b351` — `compare_search_modes.py` (standard vs Super Search).

## Harnesses / how to re-run
All under `mwmbl/rankeval/evaluation/`, run with:
`DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python -m mwmbl.rankeval.evaluation.<name> ...`
- `compare_search_modes --fraction 0.05` — standard vs Super Search.
- `compare_ltr_models --model a=PATH --model b=PATH --fraction 0.1` — two LTR models.
- `evaluate_super_search --fraction 0.05` — Super Search alone (cached doc-pool).
- `scripts/super_search_eval.py build-matrix|select|simulate` — source-selection eval
  (reward-matrix tooling; `super_search_select/evaluation.py` has `coverage_at_k`,
  `simulate_baselines`, `sweep_explore_scale`).
- Dataset refresh: `python -m mwmbl.rankeval.dataset.extension_dataset` (needs
  `MWMBL_KEY_ID`/`MWMBL_APPLICATION_KEY` in `.env`).
- The 500-query reward matrix used above is saved (untracked) at
  `devdata/ss_eval_matrix.{npz,json}` — load with
  `RewardMatrix.load("devdata/ss_eval_matrix")` (from
  `super_search_select/evaluation.py`). To rebuild from scratch: `build-matrix`
  (~20 min; ArXiv 429s are harmless).

## Open decision for next session
Pick the direction for Super Search source selection:
- **(a) Ship the coverage-based win now** — flip the bandit on
  (`SUPER_SEARCH_USE_BANDIT=True`, `SUPER_SEARCH_TS_EXPLORE_SCALE=0.25` in
  `settings_common.py`), or replace it with a simpler non-contextual "rank sources by
  learned contribution" (captures ~96% of the bandit, far less machinery). Optionally
  add the high-precision domain-token force-include rule.
- **(b) Invest in real contextual features** (higher effort) — query-intent →
  per-source `field` affinity, or historical per-source contribution by query class.
  The three obvious topical-match features are confirmed dead ends.

Caveat on (a): the online bandit cold-starts and learns over live traffic; it won't
jump to 0.80 instantly, but will quickly leave the near-random cosine baseline behind.
