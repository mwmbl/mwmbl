# Super Search: gold-grounded source selection — findings

_Branch `super-search-intent-feature`, June 2026._

## Why this exists

Source selection (which ~10 of ~134 sources to query per request) has so far been
evaluated against a **circular proxy**: `scripts/super_search_eval.py build-matrix`
rewards a source by the fraction of its results that survive into the LTR model's
*own* top-K (`devdata/ss_eval_matrix.*`). That measures agreement with the ranker,
not real relevance.

This is the Phase-2 step foreshadowed in `scripts/super_search_coverage.py`: build a
**gold-grounded** reward matrix and selector, so we score a source by whether it
actually contains a *gold-relevant* result. The Phase-1 coverage gate already warned
the ceiling is low (~0.36% of gold mass on our source domains); this work measures the
headroom inside that subset with a real selector.

## What was built

- `scripts/super_search_eval.py build-gold-matrix` — fully **offline** (no network).
  Reads the LTR dataset (`devdata/rankeval-2026-04/learning-to-rank.csv.gz`), maps each
  row's URL to its source by registrable domain, and writes a `RewardMatrix`
  (`devdata/ss_gold_matrix.*`) where:
  - `mask[q, s]` = source `s` appears in query `q`'s LTR candidate rows (availability),
  - `R[q, s]` = **1.0 if any of those rows is gold-relevant** (non-null
    `gold_standard_rank`), else 0.0 (binary has-gold).
  - Cosine-feature source profiles are accumulated from the rows' title/extract text.
- Shared domain helpers factored into
  `mwmbl/tinysearchengine/super_search_select/domains.py` (`host_of`, `registrable`,
  `source_domain_map`), reused by `super_search_coverage.py`.
- Evaluation reuses the existing `select` / `simulate` subcommands unchanged.

## The shape of the data

LTR dataset: 252,003 rows / 2,096 queries.

- **In-coverage queries** (≥1 candidate on a source domain): **2,009 (96%)** — most
  queries do touch a source domain (github/stackoverflow/etc. are everywhere).
- **Queries with a gold-bearing source: 56 (2.8%)** — this is the hard ceiling, and it
  matches the gate's ~0.36% gold-mass finding.
- Availability is *not* sparse: mean 6.16 sources/query (median 6, max 23), so top-1/2/3
  selection has genuine room to act.
- Every gold query has **exactly one** gold-bearing source (56 gold cells / 56 queries).

Where the 56 gold queries live (gold-query count / availability):

| source | gold queries | available in |
|---|---:|---:|
| www_gov_uk | 24 | 58 |
| imdb | 15 | 62 |
| github | 5 | 1,772 |
| devblogs_microsoft_com | 5 | 64 |
| genius_com | 3 | 46 |
| duckduckgo_com / nist / space / arxiv | 1 each | — |

The recoverable gold is **concentrated in gov.uk + imdb (39 of 56)** — the two sources
added in `0d10c78` precisely for their gold mass. github is available in 1,772 queries
but is the gold answer in only 5 (very low precision).

## Results

### Held-out selector quality (`select`, XGBoost grouped CV by query, coverage@k vs oracle on the 56 gold queries)

| k | coverage@k | top features (ablation drop) |
|---:|---:|---|
| 1 | 0.464 | estimated_pages +0.232, intent_news +0.089, popularity +0.036 |
| 2 | 0.696 | estimated_pages +0.286, popularity +0.089 |
| 3 | 0.768 | estimated_pages +0.196, popularity +0.054 |

A learned model can place the gold-bearing source in the top-3 ~77% of the time.

### Policy replay (`simulate`, mean captured reward / query over all 2,009; oracle = 0.0279)

| k | oracle | TS (best ν) | cosine | random | popularity |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.0279 | **0.0184** (ν=0.25) | 0.0070 | 0.0050 | 0.0035 |
| 2 | 0.0279 | **0.0249** (ν=0.5) | 0.0124 | 0.0129 | 0.0075 |
| 3 | 0.0279 | **0.0254** (ν=0.5) | 0.0169 | 0.0139 | 0.0090 |

The learned bandit beats the cosine baseline by ~2.6× at k=1 and reaches ~91% of oracle
by k=3.

## Interpretation

1. **The ceiling is tiny — 56 queries (2.8%).** Even a perfect selector moves relevance
   on <3% of queries, with bounded NDCG upside. This corroborates the coverage gate and
   the add-sources track (`SUPER_SEARCH_ADD_SOURCES_FINDINGS.md`).

2. **The production cosine signal is misaligned with gold.** Across every k, the gold
   reward is driven by `estimated_pages` and `popularity` (source size/quality priors),
   while `cos_bow` / `cos_cng` — the features the live `_select_cosine` baseline ranks on
   — have ~zero or **negative** ablation. Query↔source cosine does not predict which
   source holds the gold result.

3. **"Learned beats cosine" is really "always include gov.uk + imdb."** The gold mass is
   carried by gov.uk and imdb, each available in only ~60 queries, and every gold query
   has a single gold source. A learned policy succeeds by learning a near-static per-source
   prior (favour the high-gold sources whenever present) — which is exactly what
   `SUPER_SEARCH_FORCE_INCLUDE` already does for them.

## Recommendation: static, not learned

The data says go static, consistent with the gate's stated default:

- **Force-include the handful of high-gold sources** (gov.uk, imdb already in
  `SUPER_SEARCH_FORCE_INCLUDE`; consider github/devblogs by size prior) rather than
  shipping a per-query learned selector — 56 single-source gold queries do not justify a
  contextual model.
- **If selection is tuned at all, rank by `popularity`/`estimated_pages`, not cosine** —
  the cosine baseline optimizes a signal uncorrelated with gold relevance.
- **Keep investing in adding high-gold-mass domains** (the add-sources track) — that, not
  selection among existing sources, is where the gold mass is.

## Caveats

- 56 gold queries → small CV folds; absolute coverage@k numbers are noisy and
  `estimated_pages` dominance partly reflects that gold-bearing sources happen to be large
  (gov.uk, imdb, github). Directional, not precise.
- Reward is binary has-gold; it ignores *where* in gold the URL ranked. A graded variant
  would refine the within-subset story but not the ceiling.

## Reproduce

```
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python scripts/super_search_eval.py build-gold-matrix --out devdata/ss_gold_matrix
uv run python scripts/super_search_eval.py select   --matrix devdata/ss_gold_matrix --k 2
uv run python scripts/super_search_eval.py simulate --matrix devdata/ss_gold_matrix --k 2
```
