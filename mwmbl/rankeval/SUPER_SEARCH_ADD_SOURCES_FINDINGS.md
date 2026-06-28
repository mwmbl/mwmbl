# Super Search: adding high-gold-mass domains as sources — findings

_Branch `super-search-intent-feature`, June 2026._

## Why this exists

Earlier work tried to improve Super Search NDCG by tuning **source selection**
(which ~10 of ~130 sources to query) — a contextual bandit, then an offline
gold-grounded selector. A feasibility gate killed that framing: our current source
domains carry only **~0.36% of the gold relevance mass** (rankeval train+test,
click-weighted top-10), so no selection policy can move the needle. See
`SUPER_SEARCH_EVAL_FINDINGS.md` and `scripts/super_search_coverage.py`.

The inverse question — *which high-value domains are we missing?* — pointed at
adding sources instead of selecting them.

## Where the gold relevance actually lives

Ranking gold domains we **don't** have as sources, by click-weighted mass
(`scripts/super_search_coverage.py` inverse analysis):

- Current sources (registrable match): **~1%** of gold mass.
- Top-10 missing domains: **26.6%**; top-20: **31.8%**; top-40: **37.6%**.

Top 25 missing domains (rankeval train+test, before gov.uk/imdb were added —
`scripts/super_search_coverage.py missing`). `mass` is click-weighted gold relevance
(top-10 positions only); `rank1-3` is how many of the hits sat at ranks 1–3.

| # | domain | mass | %mass | queries | rows | rank1-3 |
|---:|---|---:|---:|---:|---:|---:|
| 1 | wikipedia.org | 1827.6 | 11.4% | 6300 | 11512 | 7401 |
| 2 | instagram.com | 428.2 | 2.7% | 2976 | 4948 | 1695 |
| 3 | imdb.com ✅ | 348.1 | 2.2% | 1648 | 2755 | 1717 |
| 4 | reddit.com | 327.3 | 2.0% | 3017 | 4301 | 1057 |
| 5 | facebook.com | 302.4 | 1.9% | 2918 | 4488 | 826 |
| 6 | youtube.com | 247.8 | 1.5% | 1966 | 3478 | 683 |
| 7 | google.com | 245.2 | 1.5% | 1096 | 2839 | 806 |
| 8 | bbc.com | 235.1 | 1.5% | 1382 | 2278 | 855 |
| 9 | gov.uk ✅ | 196.8 | 1.2% | 474 | 1213 | 848 |
| 10 | rottentomatoes.com | 111.4 | 0.7% | 944 | 1352 | 402 |
| 11 | amazon.com | 96.1 | 0.6% | 828 | 1011 | 335 |
| 12 | apple.com | 91.1 | 0.6% | 737 | 1307 | 237 |
| 13 | theguardian.com | 89.5 | 0.6% | 761 | 1105 | 289 |
| 14 | tripadvisor.com | 89.2 | 0.6% | 714 | 968 | 365 |
| 15 | nhs.uk | 85.6 | 0.5% | 397 | 805 | 342 |
| 16 | netflix.com | 82.3 | 0.5% | 473 | 862 | 348 |
| 17 | x.com | 78.7 | 0.5% | 860 | 1231 | 195 |
| 18 | espn.com | 77.3 | 0.5% | 553 | 893 | 288 |
| 19 | merriam-webster.com | 73.9 | 0.5% | 404 | 547 | 317 |
| 20 | skysports.com | 71.9 | 0.4% | 419 | 746 | 295 |
| 21 | mayoclinic.org | 67.1 | 0.4% | 255 | 429 | 268 |
| 22 | cambridge.org | 66.7 | 0.4% | 389 | 512 | 252 |
| 23 | spotify.com | 58.5 | 0.4% | 463 | 732 | 187 |
| 24 | fandom.com | 58.2 | 0.4% | 447 | 579 | 205 |
| 25 | britannica.com | 58.0 | 0.4% | 499 | 679 | 241 |

✅ = added this pass. After removing already-have (Wikipedia, row 1) and unaddressable
login-walled/social (instagram, facebook, x, and further down tiktok/linkedin/pinterest
≈ 6.5%) and google-self-links (row 7, 1.5%), **~15–18% of gold mass sits on domains with
real, scrapeable search** (reddit, bbc, gov.uk, rottentomatoes, guardian, the
dictionaries, nhs/mayoclinic, …). Many, however, are **IP-blocked (403)** from a
datacenter — see Source recall below.

## What was added

Two sources — the biggest *accessible* missing domains:

- **`www_gov_uk`** — recipe YAML over the GOV.UK search API.
- **`imdb`** — hand-written adapter over IMDb's autosuggest API (query goes in the
  URL path, so it can't be a declarative recipe).

Supporting infra:

- Recipe engine gained a JSON **`base_url`** join (`recipe.py`): a JSON API that
  returns a relative path (gov.uk's `/contact-hmrc`) is joined to the canonical
  absolute URL. Needed because the URL-template helper %-quotes slashes and so
  can't path-join.
- **`SUPER_SEARCH_FORCE_INCLUDE`** setting + pinning in `select_sources` (minimal
  selection wiring; the bandit stays dormant). Defaults to `[]` — **off**.
- `scripts/super_search_coverage.py recall --source X` — per-source exact-URL recall.
- `scripts/super_search_new_sources_eval.py` — NDCG over the in-coverage subset.
- `evaluate_super_search._collect_docs` now takes a `selection_key` so the doc-pool
  cache never mixes source configurations; `compare_search_modes` is 3-way + `--clear-cache`.

## Source recall (does the source return the *exact* gold URL?)

The NDCG harness matches URLs by exact string, so this is the load-bearing metric.

| source | exact-URL recall | near-match (host+path) |
|---|---|---|
| gov.uk | **58%** | 58% (URLs already canonical) |
| imdb | **42%** | **67%** |

imdb's 25-pt exact/near gap is entirely `m.imdb.com` (34% of imdb gold) vs our
`www.imdb.com` (66%) — we emit the majority form; one string can't match both.

Many other high-value domains are **IP-blocked (HTTP 403)** from a datacenter:
reddit (also needs OAuth now), merriam-webster, britannica, mayoclinic. Cambridge
and Cleveland Clinic are reachable but were not added in this pass.

## End-to-end NDCG (in-coverage test subset, gov.uk+imdb force-included)

`scripts/super_search_new_sources_eval.py` — only the test queries whose gold
results include a URL on a new source's domain (1,313 such queries), where
selection can actually change the ranking.

| arm | NDCG (n=50) | NDCG (n=150) |
|---|---|---|
| standard search | 0.516 | 0.515 |
| Super Search baseline | 0.463 | 0.485 |
| **Super Search + new sources** | **0.477** | **0.488** |
| Δ (new − baseline) | +0.014 | **+0.003** |
| queries better / worse | 7 / 6 | **16 / 31** |

## Conclusion

**Adding good sources nets ≈ 0 on NDCG, and Super Search still trails plain
standard search (0.488 vs 0.515) even on the queries where the new domains are
relevant.**

The mechanism works in isolation — the new sources produce large, real individual
wins (gov.uk takes `inheritance tax threshold` and `login universal credit` to
NDCG 1.0; imdb lifts `stealing paradise` +0.63), proving the chain
source → fetch exact gold URL → survive LTR → top-10. But at n=150 those wins are
**outnumbered ~2:1 by regressions** (16 vs 31).

Critically, the 31 regressions are on queries where the new domain *is* relevant —
so they are **not** caused by off-topic sources. They are the **LTR re-ranker
mis-ranking the enlarged document pool**: it promotes a new-source doc that isn't
the gold answer above a previously-better result.

So the bottleneck is **the re-ranker over the heterogeneous Super Search pool**
(external results + crawled pages + link-followed pages) — not source availability
and not source selection. That re-ranker is also why Super Search (0.49) trails
standard search (0.52) overall.

## Recommendations

1. **Do not ship `SUPER_SEARCH_FORCE_INCLUDE`** (force-including is net-harmful).
   It defaults to `[]`, so production behaviour is unchanged. The gov.uk/imdb
   adapters are sound and remain available for when the ranker improves.
2. **Next lever: the LTR re-ranker.** Retrain / filter it on the heterogeneous
   Super Search document pool against gold, where the 0.49-vs-0.52 gap lives.
   Adding sources without fixing the ranker does not pay off.

## Reproducing the results

**Prerequisites**
- Gold dataset present at `devdata/rankeval-2026-04/remote-datasets/rankings-{train,test}.csv`
  (rebuild with `uv run python -m mwmbl.rankeval.dataset.extension_dataset` if missing).
- Local mwmbl index at `devdata/index-v2.tinysearch` (the standard-search arm uses it).
- Network access — the sources are live APIs — and Redis (the cosine selection path).
- Every command is prefixed with the env both scripts/harnesses expect:
  `DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev`.

Let `E=DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev` below.

**1. Gold-coverage gate (the ~0.36% mass ceiling that killed source selection)**
```
$E uv run python scripts/super_search_coverage.py            # report (default)
```

**2. Missing-domain ranking (the addressable upside that motivated adding sources)**
```
$E uv run python scripts/super_search_coverage.py missing --top 45
```
NB: the "Where the gold relevance lives" figures (top-10 = 26.6%, current sources ≈ 1%)
were measured *before* gov.uk/imdb were added. Re-running now drops those two from the
list and raises the already-a-source share to ~4.4% (top-10 → ~24%). To get the exact
pre-addition numbers, run this against the parent commit (`git stash` / checkout `HEAD~1`).

**3. Per-source exact-URL recall against gold (the load-bearing match metric)**
```
$E uv run python scripts/super_search_coverage.py recall --source www_gov_uk --max-queries 60
$E uv run python scripts/super_search_coverage.py recall --source imdb       --max-queries 60
```

**4. End-to-end NDCG on the in-coverage subset (the headline table) — the result to reproduce**
```
$E uv run python scripts/super_search_new_sources_eval.py --max-queries 50    # faster read
$E uv run python scripts/super_search_new_sources_eval.py --max-queries 150   # ~25-40 min
```
Each invocation force-includes `www_gov_uk`+`imdb` internally and prints a `mean NDCG`
block for the three arms (`standard`, `ss-baseline`, `ss+new`) plus the per-query
better/worse breakdown. The headline table is those two runs side by side — the
`--max-queries 50` run is the n=50 column, `--max-queries 150` the n=150 column:

| arm | NDCG (n=50) | NDCG (n=150) |
|---|---|---|
| standard search | 0.516 | 0.515 |
| Super Search baseline | 0.463 | 0.485 |
| **Super Search + new sources** | **0.477** | **0.488** |
| Δ (new − baseline) | +0.014 | +0.003 |
| queries better / worse | 7 / 6 | 16 / 31 |

Slow because it runs the full Super Search pipeline (live source fan-out + crawl +
link-follow) per query. Absolute numbers drift run-to-run (live APIs, crawl variability);
the query *sample* is seeded (`default_rng(0)`) so the same queries are scored each time,
and the signal to trust is the paired `ss+new − ss-baseline` delta and the better/worse count.

**5. Full-sample 3-way comparison (slower, and too diluted to show the effect — for context)**
```
$E uv run python -m mwmbl.rankeval.evaluation.compare_search_modes --fraction 0.03 --clear-cache
```

**Unit tests for the new code**
```
$E uv run pytest test/test_super_search_sources.py test/test_super_search_recipes.py \
                 test/test_super_search_select.py -q
```

**Caveats** — absolute NDCG and recall drift run-to-run (live APIs, crawl variability,
Wikipedia availability); trust the *paired* deltas, not absolute values. The recall
sampler and the in-coverage eval subset are both seeded (`Random(0)` / `default_rng(0)`),
so the *query selection* is reproducible; the fetched results are not. The `--clear-cache`
flag in step 5 matters: the doc-pool cache is keyed by query + selection set, so stale
pools must be cleared when source adapters or the force-include set change.
