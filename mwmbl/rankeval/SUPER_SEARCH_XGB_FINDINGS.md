# Super Search XGBoost contextual bandit — offline findings

Decision record for replacing cosine / per-arm LinTS source selection with a
batch-trained XGBoost contextual bandit (`SUPER_SEARCH_SELECTION_MODE=xgb`):
pointwise reward model over the shared context features ⊕ a source-identity
one-hot, epsilon-greedy exploration, retrained daily from the privacy-safe
impression log (`xgb_model.py`, `rstats.py`).

## Gate check on the old matrices (survival / gold rewards)

`scripts/super_search_eval.py simulate-xgb`, k=10, mean captured reward per
query (single-pass replay; LinTS updates per request, XGB refits every 50
queries with the reward-EMA feature recomputed along the replay):

### ss_eval_matrix_intent (7297 autocomplete queries, LTR-survival rewards)

| policy                    | captured reward |
|---------------------------|-----------------|
| oracle                    | 0.992 |
| LinTS (nu=0.05)           | **0.849** |
| xgb (eps=0.05, +EMA)      | 0.785 |
| xgb (eps=0.05, pre-EMA)   | 0.734 |
| popularity                | 0.579 |
| cosine                    | 0.431 |
| random                    | 0.423 |

Notes:
- The per-source reward EMA (now wired as the `contribution_ema` feature via
  `rstats.py`) closed a third of the LinTS gap: 0.734 → 0.785.
- Ruled out as explanations for the rest: refit cadence (refit 200→50→25
  plateaus at ~0.77–0.79) and model capacity (400 trees × depth 8 is no
  better than 200 × 6). LinTS's per-request exact per-arm least-squares
  updates are simply well matched to this matrix's strongly per-arm-linear
  survival reward.

### ss_gold_matrix (gold-label rewards)

No headroom: oracle 0.0279 vs random 0.0269 (k=10 captures nearly every
available source). LinTS 0.0279, xgb 0.0274, cosine/popularity 0.0269 — the
matrix cannot discriminate between policies. (Consistent with the earlier
gold-coverage finding that source selection moves gold NDCG on <3% of
queries.)

### Gate verdict

XGB **decisively beats cosine** everywhere but **does not beat LinTS on the
old survival-reward matrix**. Per the plan gate, cosine/LinTS removal is on
hold pending the judge-matrix comparison below — the survival reward is the
circular metric the judge pivot deliberately replaced, so the judge matrix is
the decisive eval.

## Judge matrix (per-source home queries, judge rewards) — the decisive eval

Dense matrix: 973 synthetic home queries (`devdata/ss_source_queries.json`,
5–10 per source, LLM-generated via `scripts/super_search_queryset.py`) ×
139 sources, 35,125 filled cells, reward = mean judge score of the source's
returned docs (`build-matrix --reward judge`). This replaces the
Google/extension dataset for offline training/eval; rewards match the online
reward definition exactly.

### Holdout (834 train / 139 test queries, split stratified by home source)

| policy         | coverage@10 | home-recall@10 |
|----------------|-------------|----------------|
| **xgb**        | **0.831**   | 0.361 |
| LinTS (greedy after train replay) | 0.795 | 0.347 |
| popularity     | 0.633       | 0.361 |
| cosine         | 0.594       | 0.500 |
| random         | 0.591       | 0.278 |

xgb test RMSE 0.104. Note the dissociation: cosine wins *home-source
recall* (content similarity routes home queries to their source) but is
barely above random on captured judge reward — the judge frequently scores
another source's results above the "home" source's. Reward capture, not home
routing, is the objective.

### Sequential replay (mean captured reward per query, k=10)

| policy                | captured reward |
|-----------------------|-----------------|
| oracle                | 2.992 |
| **xgb (eps=0)**       | **2.378** |
| xgb (eps=0.05)        | 2.343 |
| LinTS (best nu=0.25)  | 2.339 |
| popularity            | 1.872 |
| cosine / random       | 1.754 |

## Overall verdict

- **Cosine is dead**: at or near random on judge reward in every eval; its
  one strength (home-source recall) does not convert into reward.
- **xgb ≥ LinTS on the judge objective** (holdout +0.036 coverage, replay
  +0.004–0.039), **LinTS > xgb on the old survival matrix** (0.849 vs 0.785).
  Decision (user call, 2026-07-07): the survival reward relies on an LTR
  model trained on an unrepresentative dataset and is the circular metric the
  judge pivot replaced, so the judge results carry it — **cosine and LinTS
  removed; xgb is the only selection policy** (`SUPER_SEARCH_SELECTION_MODE`
  gone, `bandit.py` / `SuperSearchBanditState` / TS settings deleted,
  migration 0028).
- Exploration: eps=0 replays best, consistent with the earlier nu≈0.05
  finding; keep a small eps (0.05–0.1) in production anyway to de-bias the
  training log. `SUPER_SEARCH_XGB_EPSILON` defaults to 0.1.

## Warm-start artifact + Redis seed data

`mwmbl/tinysearchengine/super_search_select/artifacts/xgb/` (committed,
~1.1 MB): trained on all 35,125 judge-matrix pairs with per-source mean
reward injected into the `contribution_ema` column (train RMSE 0.093,
`reward_kind=judge` — the model actually splits on the EMA feature, gain
1.06). Served whenever the runtime dir (`SUPER_SEARCH_XGB_MODEL_DIR`) has no
artifact yet; the daily `retrain_super_search_xgb` background task replaces
it with a model trained on real impressions once ≥2000 pairs accumulate.

The artifact also carries the Redis seed data matching what the model
trained against: `profiles.npz` (batch content profiles for the 119 sources
that returned docs during the judge-matrix fetch) and
`meta.json["source_reward_means"]` (139 per-source mean judge rewards). At
startup `xgb_model.seed_online_state()` SETNX-seeds both into Redis — never
overwriting live values — so serve-time `cos_*`/`contribution_ema` features
match the training distribution from the first request, and a Redis wipe
self-heals on the next startup. Live traffic then blends over the seeds via
the decaying-mean profile updates and reward-EMA updates.

End-to-end check: the bundled model loads, selects 10 sources, records
18-dim feature vectors; on real matrix feature rows its top sources closely
track the oracle's; seeding a cold Redis is idempotent
({profiles: 119, reward_emas: 139} then {0, 0}).
