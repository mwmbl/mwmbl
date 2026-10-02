# Structural fixes for index-page evictions: NDCG estimates

Follows #459, which estimated that removing evictions entirely would add +0.06 to +0.10
index-only NDCG@10. That figure assumed the extra room brings back only the good pages
that were evicted. This estimates what concrete structural designs would add once they
also bring back everything else that was evicted, which is almost all of it.

Uses #454's en-gb set: 295 queries, UK-relevance Haiku grades, `miss_probe.json`. The
live index was read on 2026-09-27. The scripts are `scripts/combined_search_haiku/structural_*.py`
(see that directory's README), and `structural_report.py` prints the tables below.

## Summary

- **Every design lands at about +0.02 index-only NDCG@10, or less.** For production
  (Staan-first + index fill) that is about +0.001, which isn't significant.
  - The best designs give +0.024 to +0.025 [+0.010, +0.039]: a 16 KB overflow extent for hot terms,
    or document IDs on the term pages with up to 300 fetched per term.
  - The cheapest of these is the 16 KB extent.
- **Capacity past about 6× hurts.** Removing evictions entirely scores +0.016, below the
  16 KB extent.
  - The ranker lets the deep documents that come back push good results out of the top
    ten.
  - Recovering only the good evicted pages would be worth +0.084. With the rest of the
    evicted documents added back, only about 60% of the recovered good pages that reached
    the top ten still do (279 to 166, in the first run of the simulation).
- **Conjunctive (AND) retrieval doesn't avoid this.** Adding deep documents only when
  they match every query word scores +0.018.
- **So the case for a structural change is storage, not evictions.**
  - Storing each document once, with an inverted index of IDs, would shrink the index
    from 419 GB to about 65 GB.
  - Coverage is the larger loss: #454 found 76% of the lost gain is pages that aren't in
    the index at all.
- **The ranker is the other half.** Any capacity change should come with an LTR model
  retrained on the larger candidate pools. The headroom is the gap between +0.025 and
  +0.084.

## Estimates

The point estimate gives every unjudged result a gain of 1.0 (see "Unjudged results"
below). 95% bootstrap CIs over queries. Baseline: index-only 0.224, Staan-first 0.774.

| Design | Capacity for a hot term | Evicted targets recovered | Targets only: index-only | **With deep documents: index-only** | **With deep documents: Staan-first** |
|---|---|---|---|---|---|
| #459 per-page fixes (junk, dictionary, score) | ×1.2 | 6% | +0.007 | +0.003 [−0.001, +0.008] | +0.000 |
| Hot term gets its page to itself | ×1.3 | 15% | +0.016 | +0.007 [−0.001, +0.014] | −0.001 |
| 8 KB pages | ×2.3 | 35% | +0.037 | +0.016 [+0.005, +0.026] | +0.000 |
| Document IDs on 4 KB pages, fetch ≤100 per term | ×2.6 | 41% | +0.043 | +0.020 [+0.009, +0.031] | +0.001 |
| **16 KB overflow extent for hot terms** | ×6 | 69% | +0.065 | **+0.025 [+0.011, +0.039]** | +0.001 [−0.001, +0.003] |
| **Document IDs on 4 KB pages, fetch ≤300 per term** | ×8 | 77% | +0.070 | **+0.024 [+0.010, +0.039]** | +0.001 [−0.001, +0.003] |
| Inverted index, AND + ≤300 per term | ×8, plus AND matches | 79% | +0.073 | +0.023 [+0.009, +0.038] | +0.001 |
| Inverted index, AND only for multi-word queries, ≤100 per term for one word | AND matches | 44% | +0.047 | +0.018 [+0.006, +0.030] | +0.000 |
| 64 KB overflow extent for hot terms | ×21 | 100% | +0.084 | +0.017 [+0.002, +0.032] | +0.000 |
| No evictions | ∞ | 100% | +0.084 | +0.016 [+0.001, +0.031] | +0.001 |

- "Capacity" is the median multiple of the documents a hot query term keeps today.
- "Targets only" adds back just the evicted good pages, which is what #459's "+0.06 to
  +0.10" measured.
- With an unjudged gain of 0.8 or 1.2, the best designs give +0.020 to +0.021 or +0.027 to +0.028 index-only,
  and the order doesn't change.

## What each design costs

| Design | Storage | Reads per query term | Change |
|---|---|---|---|
| 8 KB pages | 838 GB (+419) | 1 × 8 KB | Rewrite the index |
| 16 KB overflow extent | ~525 GB (+105) | +1 × 16 KB for hot terms | A second file and an allocator; a page flags its overflowing term |
| 64 KB overflow extent | ~840 GB (+420) | +1 × 64 KB for hot terms | As above |
| Document IDs on 4 KB pages | ~470 GB (419 + ~50 document store) | 1 page + up to 100–300 random document reads | New document store, and every write path changes |
| Inverted index, documents stored once | ~65 GB (≈50 documents + ≈15 postings) | Posting list + up to 300 document reads | A new index format, e.g. tantivy segments |

- **Sizing:** from 1,000 random pages. 12.5% are full, and half of those have one term
  holding more than 50% of the page, so about 6.4M hot terms.
- **Document store:** documents compress to 116 bytes each in blocks of 64. With ~430M
  distinct documents, that is about 50 GB.
- **Postings:** ~2.4B entries at ~6 bytes each (ID and quantised score).

## Why capacity stops helping

- **On the pages that evict targets, the query term already fills the page.**
  - The term holds a median 79% of its page's bytes, with 38 documents.
  - So sharding pages differently, or giving the term a page to itself, buys only ×1.3.
- **The good pages are deep.**
  - Among Brave's good results that are filed under a query term (by the index's own
    stored text) and whose page is full, evicted outnumber kept by 15:1 for unigrams
    (884:59) and 2.6:1 for bigrams (203:77).
  - Pages that aren't full evict nothing (2 of 27), which confirms the probe.
  - So recovering most of them takes about ×6 or more.
- **Everything else in that deep tail comes back too.** It is mostly mediocre.
  - The 16 KB design adds about 120k documents to the lists of the 1,021 query terms.
  - Unjudged newcomers take 44% of its top-ten slots.
  - I graded a sample; their mean gain is 0.97 [0.79, 1.16], against 2.70 for the judged
    results they displace.
  - At ×20 and beyond, newcomers take 58% of top-ten slots and NDCG falls again.

## Method

- **Harness:**
  - Index-only ranking is `CombinedLTRRanker` + MMR over the live index's `/search/raw`
    results, as in #454 and #459.
  - It reproduces #459: index-only 0.148 (#459: 0.149) and 0.354 (#459: 0.356); Staan-first
    0.768; no-evictions upper bound +0.068 to +0.103.
- **Targets:** the 381 good (grade ≥ 2) Brave results that the index holds and doesn't
  retrieve, and that are filed under at least one query term. The terms come from
  `tokenize_document` on the index's stored text rather than Brave's, so they are the
  terms the document was actually filed under.
- **Per-term capacity:** the whole 4 KB page of every query term (1,021 terms) was read
  through `/search/page/{n}`, giving its fill, its documents and the query term's share.
  Each design turns that into a capacity multiple *m* for the term.
- **Recovery:**
  - A target evicted from a term comes back with probability min(1, (m − 1)/R). R is the
    measured evicted:kept ratio, 15 for unigrams and 2.64 for bigrams.
  - It is recovered if any of its evicting terms brings it back.
  - For AND designs, a target filed under every word of a multi-word query always comes
    back.
  - Twelve Monte Carlo draws per design.
- **Deep documents:**
  - For each of the 585 query unigrams on a full page, a sample of its evicted list was
    collected: documents filed under up to 25 bigrams containing the term, whose own terms
    include it, and that are not on its page. The median is 465 per term, about ×12 what
    the page keeps.
  - A bigram term's sample is the documents in its two words' samples that contain the
    bigram.
  - Each design adds back the top (m − 1)·k of the sample in `HeuristicRanker` order, the
    order the indexer keeps a term's documents in. They go into every query that looks up
    the term. Six draws per design.
- **Unjudged results:**
  - #454's judgments cover only 36% of the index-only top ten, and a design's newcomers
    are all unjudged.
  - I graded, blind and shuffled, with the UK-relevance prompt:
    - 160 random newcomers;
    - 90 unjudged baseline results;
    - 60 already-judged results mixed in for calibration.
  - Against Haiku on the calibration set: 68% identical, 98% within one grade. I grade
    slightly lower, so grades are mapped to the mean Haiku gain for each of my grades.
  - Calibrated gains:
    - newcomers 0.97 [0.79, 1.16];
    - baseline unjudged 1.08 [0.81, 1.36];
    - judged results in the baseline top ten 2.70.
  - So unjudged results get 1.0, with 0.8 and 1.2 as the sensitivity range.

## Caveats

- **The deep sample is not the true tail.** It comes from bigram pages, so it leans
  towards documents like the ones the page keeps. For ×20 and beyond the sample runs out
  (median 465), so dilution there is understated. The real "no evictions" result is
  probably lower than shown.
- **Recovery assumes independence and even spread.**
  - A target deep in "ford" is probably deep in "focus" too, so treating its terms as
    independent flatters recovery.
  - Assuming good pages are spread evenly through the evicted tail probably flatters it
    too.
- **The ranker is today's.**
  - The LTR model was trained on today's candidate pools, never on a pool ten times
    larger.
  - A model retrained on the larger pools, or a second-stage re-ranker, should do
    better. The targets-only column is the ceiling.
- **One judge for the unjudged.** The newcomer gains come from my grades calibrated to
  Haiku's on 60 items, not from Haiku itself.
  - "No evictions" only catches up with the 16 KB design at an unjudged gain of about 2,
    twice the measured value.
  - Even there, no design passes +0.043 index-only or +0.005 Staan-first.
- **Small set.** 295 queries and 381 targets, from one eval set. Production effects are
  within noise for every design.
- **Benefits not counted:**
  - What a smaller index could buy in coverage.
  - The effect on multi-word queries whose documents are on no single term's page.
