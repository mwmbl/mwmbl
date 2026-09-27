# How well the index uses its pages: a random sample

Follows #454, which found that 22% of the NDCG lost to Brave comes from pages the index
holds but has evicted from the query term's 4 KB page. This looks at 1,000 uniformly
random pages of the production index (102.4M pages, 419 GB), read on 2026-09-27 through
`GET /api/v1/search/page/{n}`. Reproduce with `uv run scripts/index_page_sample.py`.

## Findings

**1. Pages are 59% full on average, and most are not full at all.**

| | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| Compressed bytes (of 4096) | 1362 | 1754 | 2259 | 3002 | 3972 |
| Documents | 11 | 15 | 20 | 29 | 39 |
| Distinct terms | 9 | 11 | 14 | 16 | 19 |

- Only 12% of pages are full (3,900 bytes or more). So about 170 GB of the file is zero
  padding.
- A typical page holds 14 unrelated terms with 1–2 documents each. The eviction in #454
  happens on the minority of pages where a popular term shares its page with others.
- Every document's term hashes to the page it's on, so the stale-document cleanup works.
- Halving `NUM_PAGES` doesn't fix the padding: 62% of merged pairs overflow, losing 22%
  of documents.

**2. Each document is stored about 5.5 times, in full.**

- The crawl indexer files a document under up to about 29 terms: the first 10 tokens plus
  10 bigrams of each of the title, URL and extract.
- Probing 80 random documents found them on a median of 5 of their term pages (p90 = 10).
- Every copy carries the whole title, URL and extract, which are 83% of a document's
  bytes.

**3. Much of what is stored is unlikely ever to match a query.**

- 83% of the stored terms are bigrams.
- 15% of the terms look unqueryable: query strings, session IDs, hashes, or more than 40
  characters (for example `viewtopic php?t=26215&sid=8fa0…`, `57c899911 html`). They come
  from URL tokenisation, which splits only on `/`, `.` and `_`.
- 54% of the documents have an empty extract, which agrees with #454.
- 3.5% have an empty or error-page title ("Just a moment...", "403 Forbidden", "Access
  Denied").
- 3.2% have GitHub's page chrome as the extract ("Saved searches Use saved searches…").

**4. Compression is poor on pages this small.**

The ratio is 2.18 at zstd level 3. Documents that fit on a full page, from random
documents:

| Change | Docs per page | Difference |
|---|---|---|
| As now | 33.7 | — |
| zstd level 19 (ratio 2.26) | ~35 | +4% |
| Round the score to 3 d.p. (7% of raw bytes, 26% null) | 35.9 | +6% |
| Strip `https://` and `www.` from the URL | 34.9 | +4% |
| Shared zstd dictionary, 110 KB, trained on half the sample (ratio 2.62) | 40.1 | **+19%** |

## Recommendations, cheapest first

1. **Stop indexing junk.**
   - Drop documents with an empty or error-page title.
   - Replace GitHub's chrome with no extract.
   - Don't file documents under URL tokens that hold a query string or ID-like token.
   - Together these free about 15–20% of entries on a typical page. On the full pages of
     popular terms, where evictions happen, they free only about 4%.
2. **A trained zstd dictionary.** About 19% more documents per full page. Needs a
   versioned dictionary, stored in the index metadata, and a migration.
3. **Round the score and strip the URL scheme.** About 10% together, and trivial, but
   pages need rewriting to benefit.
4. **The structural issue: fixed 4 KB pages with full copies of each document.**
   - 41% of the file is padding while the popular terms overflow.
   - Two ways to address it: overflow or chain pages for full terms, or store each
     document once and put IDs on the term pages.
   - Either is a larger design change, but it's what would actually fix the #454
     evictions.

## Estimated effect on NDCG

This uses #454's en-gb set (295 queries, UK-relevance Haiku grades, `miss_probe.json`).
The 435 targets in bucket 2a ("evicted") are the only ones these fixes can bring back.

**Result: only the structural fix moves NDCG.**

| Fix | Index-only NDCG@10 | Staan-first + index fill (production, 0.768) |
|---|---|---|
| Junk removal | +0.001 | +0.0001 |
| zstd dictionary | +0.004 to +0.006 | +0.0003 |
| Score and scheme | +0.001 | +0.0001 |
| All three | +0.005 to +0.008 | +0.0004 [+0.0000, +0.0007] |
| Structural (no evictions) | **+0.06 to +0.10** | **+0.004 to +0.006** |
| Junk filtered out of results | +0.000 [−0.002, +0.002] | +0.000 to +0.001 |

The index-only baseline is 0.149 to 0.356, depending on the treatment of unjudged
results.

### Why the per-page fixes do so little

- **They free less space where it matters.** 367 of the 370 query-term pages that evicted
  a target are full. On those pages, the extra capacity is:
  - junk removal: +4% (popular terms' pages carry little junk);
  - dictionary: +15%;
  - score and scheme: +3%;
  - all three: +19.5%.
- **The queue behind a full page is long.** Among Brave's good results that the index
  holds and that should be filed under a query term, evicted ones outnumber kept ones by
  15:1 for unigram terms and 2.2:1 for bigram terms.
  - So +19.5% capacity brings back only about 1.3% of evicted relevant pages on a unigram
    term, and about 9% on a bigram term.

### Method

- **Index-only ranking:** `CombinedLTRRanker` + MMR through `RemoteIndex`, re-run on
  2026-09-27.
  - 100 of #454's 101 controls are still in the top ten.
  - The Staan-first arm reproduces the published 0.768.
- **Structural fix (upper bound):** every 2a target is injected, with the text the index
  stored for it, into its query-term pages, and all queries are re-ranked.
- **Per-page fixes:**
  - Each target is injected alone to get its NDCG gain.
  - That gain is weighted by the chance the fix brings the target back: 1 − Π(1 − x/R)
    over its eviction terms, where:
    - x is the measured extra capacity of that term's page;
    - R is the evicted:kept ratio for the term's type.
  - This assumes relevant pages are spread evenly just past a page's cut-off.
- **Junk filtered out of results:** junk documents are removed at retrieval and the
  queries are re-ranked.

### Caveats for the estimates

- **Unjudged results:** only 36% of the index-only top ten is judged. Each range runs from
  unjudged = 0 to unjudged = the mean judged gain.
- **Target grades:** targets are graded on Brave's text. The index's own text lowers the
  structural gain by about 0.013.
- **Estimated recovery rate:** the evicted:kept ratio comes from 1,288 (page, term) pairs.
  It is noisy, and it probably flatters recovery, because relevant pages are likely
  denser at the top of a page than just past its cut-off.
- **Excluded benefits:** none of this counts a smaller index or faster reads.

## Caveats

- The replication probe only recognises the crawl indexer's terms. Copies filed under
  query terms by `index_results_against_query` are missed, so 5.5 is a lower bound.
- "Unqueryable" is a heuristic on the term's text, not measured against a query log.
- 1,000 pages is 0.001% of the index. The percentiles are stable, but host-level counts
  are not.
