# Filling Staan's gaps: Staan page 2 and Wikidata

2026-09-25 · follows `combined-search-haiku-eval.md` (same directory) · every table below is
printed by `python -m mwmbl.rankeval.evaluation.wikidata_fill_report`, from data committed
under `devdata/combined_providers_eval/`.

**Question:** in `combined-search-haiku-eval.md`, the best arm was Staan-first: keep Staan's
results in Staan's order and fill the rest of the top ten from our own candidates. Why does
filling a few tail slots help so much? And could Wikidata fill them better?

## Summary

- **All of the fill's gain comes from queries where Staan returns fewer than ten.** That is
  about two thirds of queries; the average is 8.7–8.8. When Staan returns ten, staan-first + fill
  *is* Staan. The gain is large because the log discount is gentle: positions 8–10 make up
  20% of an all-perfect top ten's DCG, and an empty slot scores zero against an ideal that
  nearly always has ten good results.
- **Staan has a second page even when its first is short.** For 20 short queries, `offset=10`
  returned 9–10 results every time, nearly all new. Page 1 is being trimmed on Staan's side;
  we drop nothing. Page 2 has not been judged.
- **Wikidata helps a little, and not on top of the MiniLM fill.** Putting up to two Wikidata
  links before the index fill adds +0.003 [+0.001, +0.006] pass-3 overall NDCG (+0.019 on the
  51 queries it changes). On top of the MiniLM(LTR top 30 + Wikipedia) fill it adds +0.002
  [−0.000, +0.004], which is noise.
- **Coverage is what limits it.** Only 95 of 295 queries exactly match a Wikidata entity. Of
  the official websites found, Staan already returned the host for 28 of 40.
- **Link quality depends on the kind.** TMDB, Metacritic, Transfermarkt and MusicBrainz links
  are graded well. Social profiles (X, Facebook, Instagram, YouTube) are poor on relevance
  and on ethos.

**Recommendation:** don't build Wikidata as a gap-filler. The better candidate is Staan page 2.
Its value needs judging with the same prompts before paying for a second call per short query.

## Method

- **Queries and judges** are as in `combined-search-haiku-eval.md`. The en-gb run is used
  throughout, apart from the fill-gain table, which also shows en-us.
- **Staan page 2:** 20 random en-gb queries whose stored Staan list was short. For each, page
  1 and page 2 (`count=10`, `offset=0/10`, `market=en-gb`) were fetched raw: 40 paid calls
  (`scripts/combined_search_haiku/staan_page2.py`).
- **Wikidata:** `wbsearchentities` (top 3 items, English) and `wbgetentities` for every
  query (`scripts/combined_search_haiku/wikidata_pool.py`).
  - **Entity:** the first hit whose matched label or alias equals the query, ignoring case
    and punctuation. Search matches prefixes, so without this "arnold press" would be Arnold
    Pressburger.
  - **Links:** the entity's official website (P856), then a curated list of user-facing
    external IDs, in `LINK_PROPERTIES` order.
  - **Titles and snippets:** each link gets what a Wikidata-only result could show. The
    title is the label (plus the site name for an external ID). The snippet is the
    description.
- **Arms:** Staan in Staan's order, then up to *k* Wikidata links, then the usual fill.
  - A link is skipped when Staan already has its URL. An official site is also skipped when
    Staan already has its host.
- **Judging:** Haiku judged the 81 Wikidata URLs that no earlier judgment covered. It used both
  `relevance_engb` and pass-3, blind and shuffled. They were mixed in with 188 already-graded
  "anchor" URLs from the same 47 queries.
  - The new URLs take the new grades. Everything already graded keeps its original grade.

## Results

### What filling Staan's short lists is worth

Mean Haiku relevance NDCG@10 gain of staan-first + fill over Staan alone:

| Staan results | en-us queries | Mean gain | en-gb queries | Mean gain |
|---|---|---|---|---|
| 10 | 107 | 0 | 94 | 0 |
| 9 | 96 | +0.021 | 94 | +0.016 |
| 8 | 47 | +0.046 | 51 | +0.036 |
| 7 | 30 | +0.070 | 36 | +0.041 |
| ≤ 6 | 15 | +0.10–0.22 | 20 | +0.07–0.14 |
| **all** | 295 | **+0.027** | 295 | **+0.022** |

- The fill's mean relevance is 1.1–1.3 out of 3. Staan's own 8th–10th results, when it returns
  ten, average 2.2. A mediocre fill still beats an empty slot.
- Staan's short lists are not our doing. We drop only results with no URL or title, and none
  were found. The external-cache page had room for more on every stored list.

### Staan page 2

Of the 20 re-fetched page 1s, 14 were still short. They averaged 8.7 results, and the counts
often differ from the stored ones: Staan's ranking is not deterministic. Page 2 averaged 9.7
results, 9.4 of them not on page 1, and it was full even where page 1 had 6 or 7. The full
table is in the report output.

### Wikidata coverage (295 en-gb queries)

| | Queries |
|---|---|
| Any search hit | 111 |
| A hit exactly matching the query | 95 |
| ...with a usable link | 72 (58 of them with a short Staan list) |
| ...with an official website | 40 (Staan already had the host for 28) |

Several of the new official sites belong to the wrong sense of the query: "christmas songs"
matched a Diana Krall album, and "tourettes" matched a poet by an alias.

### Arms, pass-3 judge (288 queries; Wikidata changes the top ten of 51)

| Arm | Overall NDCG | vs staan-first + fill | ...on the 51 changed | vs MiniLM fill |
|---|---|---|---|---|
| staan-first + fill | 0.828 | — | — | −0.011 |
| + Wikidata official site | 0.829 | +0.001 [−0.000, +0.002] | +0.004 | −0.011 |
| + 1 Wikidata link | 0.831 | +0.003 [+0.001, +0.005] | +0.016 | −0.009 |
| + 2 Wikidata links | 0.831 | +0.003 [+0.001, +0.006] | +0.019 [+0.007, +0.032] | −0.008 |
| + all Wikidata links | 0.832 | +0.004 [+0.001, +0.007] | +0.021 | −0.008 |
| staan-first, fill MiniLM(LTR top 30 + Wikipedia) | 0.839 | +0.011 [+0.008, +0.015] | +0.017 | — |
| + 2 Wikidata links, then MiniLM fill | 0.841 | +0.013 | +0.026 | **+0.002 [−0.000, +0.004]** |
| brave | 0.893 | +0.065 | +0.057 | +0.053 |

- The UK-relevance judge agrees (295 queries): 2 Wikidata links add +0.004 [+0.002, +0.006]
  to the index fill, and +0.002 [+0.000, +0.004] to the MiniLM fill.
- Ethos doesn't move (1.75 / 1.78).
- These baselines differ from `combined-search-haiku-eval.md` in the third decimal because the
  query set differs. A query counts here when every URL in *these* arms is graded.

### The Wikidata URLs, by kind (newly graded only)

| Kind | URLs | Pass-3 relevance | Ethos | Overall | UK relevance |
|---|---|---|---|---|---|
| TMDB | 12 | 2.83 | 1.92 | 7.1 | 2.25 |
| X | 11 | 1.36 | 1.00 | 2.7 | 1.45 |
| official | 11 | 1.91 | 1.91 | 5.5 | 2.55 |
| Britannica | 6 | 2.00 | 2.33 | 5.7 | 2.17 |
| BBC News | 6 | 1.00 | 2.17 | 3.0 | 2.00 |
| Facebook / YouTube / Instagram | 14 | 1.57 | 1.00 | 2.9 | 1.57 |
| The Guardian | 5 | 1.60 | 2.40 | 4.8 | 1.80 |
| Metacritic | 4 | 2.50 | 2.00 | 6.5 | 2.00 |
| OpenStreetMap | 3 | 1.67 | 3.00 | 5.3 | 2.00 |
| Transfermarkt / MusicBrainz | 4 | 2.75 | 2.50 | 7.0 | 1.75 |

The counts are small. The full per-kind table is in the report output.

### Anchors: does the new judge run agree with the old one?

| Judge | Anchors | Exact | Within 1 | Mean, original → new | Correlation |
|---|---|---|---|---|---|
| UK relevance (0–3) | 188 | 62% | 99% | 1.98 → 1.85 | 0.71 |
| Pass-3 overall (0–10) | 188 | 34% | 59% | 5.63 → 5.19 | 0.75 |

The new run is slightly harsher than the original, so the new Wikidata URLs are, if anything,
under-rated against the URLs graded earlier. Correcting for that would not change the
conclusion: a shift of this size on 81 URLs is well inside the gap to the MiniLM fill.

## Caveats

- **Page 2 is unjudged.** This shows that page 2 exists, not that it beats our fill.
- **The link list was curated after looking at which properties the matched entities carry.**
  That was on the same queries, but before any grades were seen.
- **Wikidata rate-limits parallel clients.** Four parallel requests got HTTP 429s. The pool was
  fetched serially. A production source would need a cache or a local dump. `maxlag` makes
  read requests fail whenever the query service lags, so leave it off for reads.

## Reproducing

```sh
.venv/bin/python -m mwmbl.rankeval.evaluation.wikidata_fill_report
```

The generation steps, all one-off, are in `scripts/combined_search_haiku/`:

1. `staan_page2.py` (paid).
2. `wikidata_pool.py`.
3. `wikidata_arms.py`: the URLs to judge, plus anchors.
4. `make_batches_wikidata.py`: the Haiku batches.
5. `consolidate_wikidata.py`: the judge output into `haiku/*_wikidata.jsonl`.
