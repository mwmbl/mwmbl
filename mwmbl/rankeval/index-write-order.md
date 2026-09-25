# Ordering a term's index page with the LTR model: an A/B

2026-09-25 · follows `combined-search-miss-attribution.md` (same directory). The tables come
from `scripts/index_write_order/evaluate.py report`, which reads only the data committed under
`devdata/combined_providers_eval/`. The one exception is the page table, from `evaluate.py pages`,
which needs the local indexes.

**Question:** a fifth of the good Brave results the index misses are pages it holds but has
evicted from the query term's page. When a term's 4096-byte page is full, what survives is
decided by the order `index_pages` gives the documents, and today that is `HeuristicRanker`.
Its score is multiplied by `get_domain_score + 0.1`, from a list of 7.9k Hacker News domains,
so a host outside the list scores about ten times lower. On crowded pages, 95% of documents
are HN-listed, often short `/tag/<term>` pages, while 75% of the evicted targets are not.
**Does ordering the page with the LTR model instead retrieve better results?**

## Summary

- **No. LTR ordering makes index-only results worse:** Haiku NDCG@10 falls from 0.346 to 0.317,
  a difference of −0.030 [−0.044, −0.015], and good results per query fall from 3.74 to 3.45.
  LTR is better on 46 queries, worse on 68, and tied on 150.
- **The cause is spam.** Asked about a single term with no quality prior, the model rewards URLs
  whose subdomain and path repeat the term. For "pressure cooker", its top five are all pages
  like `pressurecookers02469.blogoxo.com/…`, graded 0. Top-ten results from numbered spam-blog
  subdomains rise from 23 to 129, and the share of top-ten results graded 0 rises from 29.7% to
  33.2%.
- **The HN list has been doing anti-spam work.** It is also why the query-time LTR works: the
  model was trained on pools the heuristic had already filtered.
- **LTR does fix what it was meant to fix.** Evicted targets reach the candidate set more often
  (26% to 35%), and where the heuristic floods a page with HN-listed hosts that have nothing
  to do with the query, LTR wins clearly. "cupcakes" (+7 good results) and "out" (+6) were
  full of GitHub repos and HN-favourite blogs.
- **The earlier offline estimate was wrong.** It said 87% of evicted targets would be kept, but it
  compared them only against the pages that had survived the heuristic. The spam that the
  heuristic keeps out never got to compete.
- **Writing with LTR is cheap.** It was 2.7× *faster* than the heuristic on the A/B's large
  per-term batches (506 s against 1,350 s for 6.6M documents), because the Rust features are
  faster than the heuristic's Python regexes. It is about 2.3× slower per (term, document) pair
  when each term gets only a few documents.

**Recommendation:** keep the heuristic, and keep `INDEX_PAGE_RANKER` at its default. A domain
prior is needed, but one wider than Hacker News: link authority from the crawl's link graph,
curated and approved domains, and a spam demotion for numbered blog subdomains. Then test
LTR × prior with this harness. Rebuilding the index from itself is not worth running until
then, because the same order re-evicts the same pages.

## Method

- **Corpus:** 6.6M documents, fixed for both arms:
  - every document in the local crawl batches (`devdata/batches`, 2023-11 to 2024-07, 319k
    files) that `tokenize_document` files under one of the eval queries' lookup terms;
  - the live index's current documents on those terms' pages (the RemoteIndex cache, today's
    text), and the stored text of the Brave URLs `miss_probe.py` found in the index. That
    includes the evicted targets, which survive under other terms.

  The lookup terms are every token, bigram and whole query of the 295 en-gb queries, 1,142
  terms in all (`Ranker.retrieve`). Only those pages are written, so the index is small,
  but each of them sees the full competition.
- **Arms:** `build_index.py` writes the corpus through `index_pages` in chunks of 100k
  documents, as crawl batches arrive, with `INDEX_PAGE_RANKER=heuristic` or `ltr`:
  - `heuristic`: `HeuristicRanker`, as in production today.
  - `ltr`: `LTRPageRanker`, Combined Search's model with the term as the query. Nothing is
    dropped; a full page trims from the end, as before.
- **Ranking:** `CombinedLTRRanker` + MMR with nothing from Staan over each index, as in
  `miss_probe.py`.
- **Judging:** every URL either arm puts in its top ten, shown with the text that arm's index
  stored, blind and shuffled (3,194 URLs for 269 queries; 26 queries return nothing in
  either arm). Claude Haiku 4.5 judges used the UK relevance prompt
  (`haiku/prompts/relevance_engb.txt`). Five queries the judge miscounted are left out, which
  leaves 264. NDCG@10 is scored against the ideal ordering of everything judged for the query:
  both arms here plus Brave's graded pool. The confidence interval bootstraps over queries.

## Results

| Arm | Haiku NDCG@10 | Good results (≥2) per query |
|---|---|---|
| heuristic | 0.346 | 3.74 |
| ltr | 0.317 | 3.45 |

LTR minus heuristic: −0.030 [−0.044, −0.015]. The two arms' top tens overlap by 59%.

Brave's results graded ≥2, gain-weighted as in the miss attribution:

| Set | Arm | In candidates | In top ten |
|---|---|---|---|
| all good (2,598) | heuristic | 13.8% | 9.2% |
| all good (2,598) | ltr | 14.6% | 8.2% |
| targets, missed by production (2,501) | heuristic | 7.9% | 4.3% |
| targets, missed by production (2,501) | ltr | 9.6% | 4.4% |
|   of which evicted (465) | heuristic | 25.9% | 16.1% |
|   of which evicted (465) | ltr | 35.4% | 16.8% |
| controls, in production's top ten (101) | heuristic | 100.0% | 80.6% |
| controls, in production's top ten (101) | ltr | 87.0% | 64.7% |

What the lookup terms' pages hold:

| Arm | Documents per term, median | HN-listed share | Repeated hosts per term | Empty extracts |
|---|---|---|---|---|
| heuristic | 44 | 86% | 18.4 | 39% |
| ltr | 29 | 15% | 9.4 | 31% |

LTR pages are far more diverse but hold a third fewer documents. The heuristic's URL-length
penalty packs them with short URLs, and pages from one host compress well. The median
candidate pool falls from 139 to 96.

## Rebuilding the index from itself: an estimate

The evicted documents are still in the index under other terms, so a rebuild could re-file
every unique document under all of its terms with a new order, without re-crawling.
`benchmark_rebuild.py` times this unit of work on 20k crawl documents (26 terms each) on one
core of a laptop:

| | Per document |
|---|---|
| heuristic | 4.5 ms |
| LTR | 10.3 ms |

On top of that, the scan reads 102.4M pages at about 83 µs each, about 2.4 core-hours. The
unknown is the number of unique documents. Production holds about 3.6B (term, document)
entries (102.4M pages × ~35), which is between ~140M documents (each kept under all its terms)
and ~1.2B (each kept under ~3). With the heuristic, that is **~175 to ~1,500 core-hours:
about a day to a week on 8 cores**, plus room for a second 400 GB file.
`scripts/index_write_order/census.py` is a read-only scan that counts them on production.

## Caveats

- **The corpus is mostly 2023–24 crawl text.** It is real competition for these pages,
  including what production has since evicted, but not production's exact history. Production
  has seen more documents, so its pages are more contested, not less.
- **Only the pages the eval queries read were written.** Collisions with other terms on the
  same page are rare here (1,142 terms on 65,536 pages) but common in production. Both arms are
  affected alike.
- **The URLs were judged on the index's stored text,** which is often empty (31–39%). A
  missing snippet makes a page look worse to the judge, and both arms have the problem.
