# Where the index loses Brave's good results

2026-09-25 · follows `combined-search-haiku-eval.md` and `combined-search-wikidata-fill.md`
(same directory) · every table below is printed by
`python -m mwmbl.rankeval.evaluation.miss_attribution_report`, from data committed under
`devdata/combined_providers_eval/`.

**Question:** the Haiku evals showed that the gap to Brave is recall, not ordering. When
Brave returns a good result and Mwmbl's index doesn't, which stage lost it?

1. It isn't in the index at all.
2. It's in the index, but not in the query's candidate set.
3. It was retrieved, but ranked too low.
4. It ranks fine, but the index's text makes it look worse (a snippet or extraction problem).

## Summary

- **Three quarters of the loss is pages that aren't in the index at all:** 76% of the lost
  gain [73%, 79%]. The rest is almost all stage 2.
- **Most of the remainder is pages that are indexed but pushed off the query term's page:**
  20% [18%, 23%]. A page is filed under 40–60 terms, but each term's index page holds only
  4096 compressed bytes. On a common term like "tucson" or "weather", the page is already
  full. hyundai.com/uk's Tucson page is in the index, but only under "models tucson" and
  "tucson html", not under "hyundai" or "tucson".
- **Ranking is nearly irrelevant:** candidates the ranker drops or ranks below ten cost 2%
  of the loss. The majority-terms filter costs 0.6%.
- **Extraction is a real problem, and it is tied to stage 2.** Of the target pages the index
  holds, 27% have an empty stored extract and 11% stored an error or bot-block page ("403
  ERROR The request could not be satisfied"). Re-graded on the index's own text, they fall
  from 2.73 to 2.22. The controls, the Brave pages the index already ranks in its top ten,
  barely move (2.76 to 2.68), and almost none of them have a bad extract. A likely mechanism:
  pages are ordered onto each term's page by a heuristic score, which a blank or error
  extract drags down, so these are the pages that get evicted first.
- **The missing pages are a long tail:** 1,139 hosts, and the top 25 hold only 27% of the
  missing pages. The biggest are en.wikipedia.org, bbc.co.uk, imdb.com, amazon.co.uk and
  tripadvisor.co.uk. Wikipedia is served by the separate Wikipedia source in the index-only
  v1 search, but not by Combined Search.

**Recommendation:** ranking work on the index won't close the gap. In order:

1. **Coverage:** crawl more of the high-authority hosts. They are nearly all already in the
   index, but with far too few of their pages.
2. **Per-term capacity:** stop evicting pages from common terms. Options include bigger
   pages, a second tier for overflow, or filing documents under fewer, better terms so the
   pages they are filed under aren't wasted on "html" and "(third".
3. **Extraction:** never index error or bot-block pages. Re-crawl pages whose stored extract
   is empty, and fall back to the meta description.

## Method

- **Queries:** the en-gb run of `combined-search-haiku-eval.md` (`engb/rows-0.05.json`, 295
  queries, Brave with `country=GB`).
- **Targets:** each Brave top-ten URL that Haiku graded 2 or 3 for a UK searcher
  (`haiku/relevance_engb.jsonl`) and that the index-only ranking doesn't put in its top ten.
  That gives 2,501 targets, 1,599 of them graded 3. The index-only ranking is Combined Search
  with nothing from Staan (`CombinedLTRRanker` + MMR, `RemoteIndex` against api.mwmbl.org),
  which is what production serves when Staan returns nothing. URLs are compared after
  `normalise_url`.
- **Weighting:** each target counts by the gain it has in Brave's list,
  `(2^g − 1) / log2(position + 1)`, so a bucket's share is its share of the NDCG the misses
  cost. The confidence intervals bootstrap over queries.
- **Stage 1, in the index?** A document is filed under the first ten tokens and first ten
  bigrams of its title, its URL and its extract (`tokenize_document`). For each target, the
  probe computes those terms from Brave's title and snippet and from the URL, and reads each
  term's page through `/api/v1/search/raw`: 54k distinct terms. As you suggested, this is
  searching for the page's own title and extract. If the URL is on none of those pages, it's
  "not in the index".
- **Stage 2, a candidate?** `Ranker.retrieve` for the query: every query term, bigram and
  the full-query term. A target that's in the index but not retrieved is "evicted" if its own
  terms include one of the query's lookups. Otherwise it's "filed under other words only".
- **Stage 3, ranking:** the LTR model's score for every candidate (the ranker normally
  discards it), `match_terms` from `get_features` for the majority-terms filter, and the
  target's rank in the final list.
- **Stage 4, extraction:** the title and extract the index stored for every Brave URL it
  holds (501 targets, 101 controls) were graded again with the same UK prompt, by blind Claude
  Haiku 4.5 judges. They are compared with the grade of Brave's text for the same URL. Two
  already-graded URLs per query were mixed in with their original text to measure drift. No
  query showed the same URL twice.

## Buckets

| Bucket | Targets | Graded 3 | Share of lost gain, 95% CI |
|---|---|---|---|
| blacklisted | 8 | 6 | 0.2% [0.0%, 0.7%] |
| 1b not in index, host is | 1637 | 1000 | 62.2% [59.1%, 65.3%] |
| 1c not in index, host isn't either | 355 | 226 | 13.9% [11.4%, 16.4%] |
| 2a not a candidate, filed under a query term (evicted) | 435 | 320 | 20.2% [17.6%, 23.0%] |
| 2b not a candidate, filed under other words only | 30 | 20 | 1.5% [0.8%, 2.3%] |
| 3a candidate, majority-terms filter | 12 | 9 | 0.6% [0.2%, 1.2%] |
| 3b candidate, model score <= 0 | 0 | 0 | 0.0% [0.0%, 0.0%] |
| 3c candidate, ranked below 10 | 24 | 18 | 1.3% [0.8%, 2.0%] |
| 3d candidate, dropped as a duplicate | 0 | 0 | 0.0% [0.0%, 0.0%] |

"Host is" means another page of the same host turned up on the pages probed. That is a weak
signal for big hosts, which are on some page almost always. The split by query length changes
little: long queries (5+ terms, 93 targets) are 86% not in the index.

## Extraction

Judge drift on 434 anchors (the same text, graded again): mean +0.05, 61% identical. The
controls' 15% lower against 11% higher is about the judge's noise.

| Set | Pages | Index text lower | Same | Higher | Mean Brave text | Mean index text |
|---|---|---|---|---|---|---|
| controls (ranked in top ten) | 101 | 15% | 74% | 11% | 2.76 | 2.68 |
| targets in index | 501 | 32% | 57% | 11% | 2.73 | 2.22 |
|   2 not a candidate | 464 | 33% | 56% | 10% | 2.73 | 2.19 |
|   3 candidate, ranked out | 36 | 17% | 69% | 14% | 2.75 | 2.69 |

| Stored text | Targets in index | of which graded lower | Controls | of which graded lower |
|---|---|---|---|---|
| page text | 315 | 62 | 83 | 13 |
| empty extract | 133 | 48 | 17 | 1 |
| error or bot-block page | 53 | 52 | 1 | 1 |

"Error or bot-block" is a regex over the stored title and extract (403, 404, ERROR, Access
Denied, "Just a moment", captcha, "enable JavaScript", robot), so the count is approximate.

## Caveats

- **The probe can miss a page that is in the index.** Brave's title and snippet stand in for
  the page's real text, and Brave's snippet is often query-dependent. The controls are known
  to be in the index, and 91 of 101 are found without using the query's own terms. So up to
  ~10% of "not in the index" may be in it. Those would move to stage 2, which doesn't change
  the ranking of the buckets. Hand checks of a sample against the live search found no
  wrongly-missing page (betway.com: only us.betway.com is indexed).
- **The index moves, and these are two snapshots.** The Brave results are from 2026-09-24;
  the index was probed on 2026-09-25.
- **The judge dropped one query:** it miscounted "schools near me", which was left out of
  the extraction tables.
- **The cause of eviction is inferred, not proven.** A page found in the index is found under
  a median of 3 of the terms computed from Brave's text. The rest were either evicted or never
  were its terms, because the stored text differs from Brave's.
