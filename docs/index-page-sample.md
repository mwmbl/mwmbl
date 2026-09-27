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
   - Together these free about 15–20% of entries on the pages they share.
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

## Caveats

- The replication probe only recognises the crawl indexer's terms. Copies filed under
  query terms by `index_results_against_query` are missed, so 5.5 is a lower bound.
- "Unqueryable" is a heuristic on the term's text, not measured against a query log.
- 1,000 pages is 0.001% of the index. The percentiles are stable, but host-level counts
  are not.
