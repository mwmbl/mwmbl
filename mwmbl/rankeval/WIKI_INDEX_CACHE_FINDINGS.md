# Wikipedia results from the index instead of the API

Measuring the proposal: after a call to the Wikipedia search endpoint, index the results
against the query's unigrams and bigrams; on later queries, skip the call when the index
already returned enough Wikipedia results. Goals were fewer API calls, faster results, and
deleting the filesystem HTTP cache (91,000 files / 4.5 GB on the dev box, unbounded, on the
same volume as the index, and holding the raw user query in plaintext for ten weeks).

Harness: `mwmbl/rankeval/evaluation/evaluate_wiki_index.py`. Command:

```bash
DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
  uv run python -m mwmbl.rankeval.evaluation.evaluate_wiki_index \
    --fraction 0.1 --overlay-pages 65536 --zipf 3000
```

596 queries, shuffled order, test split, over the **remote production index** unioned with
a fresh local index holding everything the run stored.

## Headline: storing is free, skipping is expensive

| arm | NDCG@10 | Δ | wiki calls | stored URLs |
|---|---|---|---|---|
| no-wiki | 0.0998 ± 0.0113 | −0.2002 | 0/596 | 0 |
| **live-wiki** (today) | **0.3000 ± 0.0172** | — | 596/596 | 0 |
| indexed-nogate | 0.3000 ± 0.0172 | +0.0000 | 596/596 | 1530 |
| indexed-raw_candidates-t1 | 0.1162 ± 0.0122 | −0.1838 | 50/596 | 89 |
| indexed-raw_candidates-t2 | 0.1216 ± 0.0124 | −0.1784 | 97/596 | 205 |
| indexed-raw_candidates-t3 | 0.1333 ± 0.0129 | −0.1667 | 148/596 | 342 |
| indexed-ranked_top_n-t1 | 0.1932 ± 0.0148 | −0.1067 | 403/596 | 991 |
| indexed-ranked_top_n-t2 | 0.2406 ± 0.0161 | −0.0593 | 503/596 | 1270 |
| indexed-ranked_top_n-t3 | 0.2703 ± 0.0167 | −0.0297 | 555/596 | 1421 |
| indexed-from_wiki_only-t1 | 0.2884 ± 0.0170 | −0.0116 | 572/596 | 1466 |
| indexed-from_wiki_only-t2 | 0.2966 ± 0.0172 | −0.0033 | 589/596 | 1515 |
| indexed-from_wiki_only-t3 | 0.3000 ± 0.0172 | +0.0000 | 591/596 | 1521 |
| indexed-unigrams-only | 0.2406 ± 0.0161 | −0.0593 | 503/596 | 1270 |
| indexed-safe | 0.2406 ± 0.0161 | −0.0593 | 503/596 | 1270 |
| never-call | 0.0998 ± 0.0113 | −0.2002 | 0/596 | 0 |

Sanity invariants hold exactly, which is the evidence the harness is wired up correctly:
`no-wiki` == `never-call`, and `live-wiki` == `indexed-nogate`.

**1. Wikipedia is worth a lot.** Live results are +0.20 NDCG over not calling at all
(0.0998 → 0.3000) — two thirds of the ranking's total score. Anything that loses them is
expensive, which is why the gate has so little room.

**2. Writing results into the index costs nothing.** `indexed-nogate` stores 1,530 URLs and
scores *identically* to `live-wiki`, to four decimal places. The stored copies never
displace a better result. So the half of the proposal that retires the disk cache is free.

**3. Every gate that saves a material number of calls loses NDCG, and there is no knee in
the curve.** `ranked_top_n` at threshold 3 is the least-bad useful setting and still loses
−0.0297 (about 1.7 SEM) while avoiding only 41/596 = 6.9% of calls. Push it to a
worthwhile saving and the loss grows roughly linearly:

| gate | calls avoided | ΔNDCG |
|---|---|---|
| ranked_top_n-t3 | 6.9% | −0.0297 |
| ranked_top_n-t2 | 15.6% | −0.0593 |
| ranked_top_n-t1 | 32.4% | −0.1067 |
| raw_candidates-t1 | 91.6% | −0.1838 |

**4. `raw_candidates` is the worst of the three, as expected.** It counts unranked
candidates, so a broad query pulls in crawled Wikipedia pages that would never have made
the top 10, and the gate fires on 546/596 queries — nearly the `never-call` floor.

**5. The damage is concentrated exactly where the feature acts.** On the subset where each
gate fired, against `live-wiki` on those same queries:

| arm | fired | NDCG (arm) | NDCG (live-wiki) | Δ |
|---|---|---|---|---|
| indexed-ranked_top_n-t1 | 193/596 | 0.1715 ± 0.0256 | 0.5011 ± 0.0329 | −0.3296 |
| indexed-ranked_top_n-t2 | 93/596 | 0.2215 ± 0.0415 | 0.6017 ± 0.0463 | −0.3802 |
| indexed-ranked_top_n-t3 | 41/596 | 0.2195 ± 0.0654 | 0.6512 ± 0.0724 | −0.4317 |
| indexed-raw_candidates-t1 | 546/596 | 0.0998 ± 0.0119 | 0.3005 ± 0.0180 | −0.2006 |

Note the direction: the *higher* the threshold, the worse the fired-subset loss. The gate
is anti-correlated with the truth. Queries where the index looks richest in Wikipedia
content are queries where live Wikipedia does *especially* well (live-wiki scores 0.65 on
the t3-fired subset versus 0.30 overall) — so "the index already has wiki results" is
close to the opposite of the signal we want.

**6. The privacy restrictions are free — at this scale, for the wrong reason.**
`indexed-unigrams-only` (bigrams never stored) and `indexed-safe`
(`WIKI_INDEX_REQUIRE_EXISTING_TERM`) are byte-identical to plain `ranked_top_n-t2`: same
NDCG, same 503 calls, same 1,270 stored URLs. That is not evidence they are harmless — it
is evidence that **what we store barely drives the gate at all**. `from_wiki_only`, which
counts only documents a previous call stored, fires on just 24/596 queries at threshold 1.
The other gates are firing on Wikipedia pages that were already in the crawled index.

**7. ~6% of fetches can never become a hit.** Wikipedia spell-corrects and partial-matches,
so 34/596 results sets contained none of the query's words and the containment rule stored
nothing for them.

## The repeat-weighted stream: the case the gold set cannot show

The gold set has **no repeated queries** — 596 unique queries, each asked once. So every
gate firing in the table above is a *term-overlap* firing: the index answered with
Wikipedia pages that merely look related. Production traffic is mostly repeats, and a
repeat is a completely different case: the index holds the very URLs the API returned last
time, so serving them is a cache hit, not an inference.

Replaying the same queries as a Zipf-weighted stream of 3,000 positions and scoring each
one, split on whether the query had been seen before:

| arm | calls | NDCG on repeats | NDCG first-seen | Δ vs live on repeats |
|---|---|---|---|---|
| live-wiki | 3000/3000 | 0.2190 ± 0.0070 | 0.3004 ± 0.0193 | — |
| indexed-ranked_top_n-t2 | 1551/3000 (48.3% avoided) | 0.1884 ± 0.0066 | 0.2379 ± 0.0180 | −0.0306 |
| indexed-from_wiki_only-t1 | 1488/3000 (50.4% avoided) | 0.2073 ± 0.0069 | 0.2876 ± 0.0190 | −0.0117 |

The Zipf weighting is a modelling assumption, so the absolute 50% is not a measurement of
production. What is informative is the **loss per call avoided**, and it separates the
gates sharply:

- `ranked_top_n-t2` avoided 15.6% of calls for −0.0593 on the gold set, but 48.3% for
  −0.0306 on repeats. Most of its firings on real traffic would be repeats, which are
  cheap; its term-overlap firings are what cost.
- `from_wiki_only-t1` avoided 4% of calls for −0.0116 on the gold set, and **50.4% for
  −0.0117 on repeats**. Its cost barely moves as the savings go from 4% to 50%, because
  the extra firings are all genuine repeats.

That is the whole result in one line: **counting the query's own stored results scales;
counting related Wikipedia content does not.**

## Storage fidelity: part of the measured loss was a bug, not the idea

A live Wikipedia result carries `score = 3.0/2.0/1.0` — its rank in Wikipedia's own
results — and `LTRRanker.order_results` feeds `score` straight to the model as a feature.
`index_results_against_query` did not copy it, so every stored copy scored `None` -> `0.0`.
A result served from the index was therefore handicapped, in a feature the ranker uses,
against the *identical* result fetched live. Fixed by `WIKI_INDEX_KEEP_SCORE`.

Same harness, `from_wiki_only` at threshold 1, repeat-weighted stream:

| arm | calls avoided | NDCG on repeats | Δ vs live on repeats |
|---|---|---|---|
| live-wiki | 0% | 0.2202 ± 0.0070 | — |
| indexed-noscore (before) | 50.4% | 0.2085 ± 0.0069 | −0.0117 |
| **indexed-from_wiki_only-t1 (after)** | **54.6%** | **0.2144 ± 0.0069** | **−0.0058** |

Both directions improved at once: the loss halved *and* more calls were avoided, because
stored copies now reach the top 10 often enough for the gate to recognise them. On the gold
set the fired-subset loss also improved, −0.2872 -> −0.2154.

This matters for how the rest of this document should be read. The gate arms were partly
measuring a storage defect rather than the policy. Three fidelity gaps remain untested and
all push the same way:

- **3 results per call.** `num_wiki_results=3`, so a cache entry holds three results where
  live Wikipedia offers many more. Nothing here tested fetching and storing more.
- **The query-chosen snippet.** The stored `extract` is the snippet Wikipedia picked
  *because it matched that query* — arbitrary as permanent index content, and it is what
  `_document_token_set` tokenises, so it also decides what the document can be filed under.
  `b39e9f8` refetches the query-independent intro instead.
- **Filed only under query terms.** Wikipedia pages are never indexed under their *own*
  tokens the way a crawled page is, so a page one person's query surfaced is not findable
  by anyone else's words.

Methodology note: `live-wiki` scored 0.3000 in the main run and 0.3016 here. The remote
production index is live and changes between runs, so only *within-run* comparisons are
valid — which is why every table above reports Δ against a baseline run at the same time.

## What this does *not* measure

- **Latency.** The harness's wall clock is meaningless here: with `RemoteIndex` each query
  makes one HTTP call per term to api.mwmbl.org, and the wiki fetch is disk-cached. Derive
  any latency claim from calls avoided × separately measured live Wikipedia call latency.
- **Eviction.** The overlay index starts empty. Production pages are >90% full and
  `_write_page` silently truncates an oversized page's tail, so there each stored document
  evicts about as much as it adds. Measure page occupancy on the production index before
  storing anything at volume.
- **Staleness.** A single run has no time axis, so it says nothing about a TTL.
- **The real repeat rate.** The Zipf stream is synthetic. Production query-frequency data
  would replace it and turn the ratio above into an actual expected saving.

## Confirmation at 1790 queries: the zero did not replicate

The zero-loss result above rested on 9 fired queries out of 596. Re-run at three times the
sample, same harness, same seed:

| arm | NDCG@10 | Δ | fired | Δ on fired | wiki@10 | calls |
|---|---|---|---|---|---|---|
| live-wiki | 0.3026 ± 0.0100 | — | — | — | 1.24 | 1790/1790 |
| indexed-nogate | 0.3021 ± 0.0100 | −0.0005 | — | — | 1.31 | 1790/1790 |
| indexed-from_wiki_only-t1 | 0.2806 ± 0.0098 | −0.0220 | 182 | −0.2163 | 1.24 | 1608/1790 |
| indexed-term_coverage-t1 | 0.2972 ± 0.0099 | −0.0054 | 35 | **−0.1927** | 1.30 | 1755/1790 |

`term_coverage = 1.0` went from +0.0000 to −0.0054 overall, and on the 35 queries it fired
on it loses −0.1927 - in line with every other gate. **There is no gate that avoids calls
without paying on the queries it fires on.** The overall figure stays small only because it
fires on 2% of queries; "small because it rarely acts" is not "free".

### Storing is not free once the index is dense

`indexed-nogate` makes every API call and still loses 0.0210 on the repeat stream. It is
not the gate - it is the storing. Wikipedia's share of the top 10 goes from 1.24 to 1.31,
displacing non-wiki gold results.

| arm | Δ on repeats | wiki@10 |
|---|---|---|
| indexed-nogate (calls every time) | −0.0210 | 1.31 |
| indexed-term_coverage-t1 | −0.0314 | 1.30 |

The effect is invisible on the gold set (−0.0005) and appears on the repeat stream. The
difference is index **density**: 2,000 positions over 596 queries stores far more per term.
Production density only increases. (Not paired-tested; treat as a strong signal, not a
settled number.) The earlier claim that storing is free was true of a sparse index only.

## What the gate is actually doing

Tracing which earlier query stored the documents that let a later query skip its call
(no network; replayed from the cached responses):

- On the repeat-weighted stream, of 797 firings **794 were the same query asked again and
  3 were a different query.** The rule is an exact-query cache implemented via terms.
- On the gold set, where nothing repeats, it fires on 7/596 = 1.2%. Six of the seven are
  single-word queries whose word was stored by a longer query containing it
  (`connections` <- `connections hint september 14`, `jobs` <- `shunter jobs near me`).
  The only multi-word case, `rachel reeves`, worked because `rachel reeves news` had stored
  the bigram.
- It catches **794 of 1,597 repeat positions - 49.7%**. A repeated query still needs *every*
  term covered, and the stored documents often do not contain all of them. Misses by query
  length: 2-token 436, 3-token 232, 4-token 80. The bigram is what fails, and 2-token
  queries are the most common length in the gold set.

Cross-query matching does not scale: requiring all unigrams *and* bigrams makes bigrams the
bottleneck, and distinct bigrams grow nearly linearly with volume. Gold-set fire rate by
decile is flat noise (0%, 2%, 0%, 2%, 0%, 2%, 2%, 0%, 0%, 5%). Repeat matching does grow
and then saturates (22% -> 47% by decile), at a ceiling set by how repetitive the traffic
is - a property of the traffic, not of the dataset size.

## Recommendation

The proposal splits into two halves with different verdicts, and the gate splits again.

**1. Take the storage half — unconditionally.** Writing Wikipedia results into the index is
free in NDCG (finding 2: identical to four decimal places), and it is what lets the
filesystem HTTP cache — unbounded, on the index's volume, holding raw queries in
plaintext — be deleted. Worth doing on its own merits.

**2. Reject the `raw_candidates` and `ranked_top_n` gates.** Neither preserves NDCG at any
useful saving, and finding 5 says why: their signal is *anti-correlated* with when
Wikipedia is needed. Queries where the crawled index looks richest in Wikipedia content
are queries where live Wikipedia does especially well (live-wiki scores 0.65 on the
t3-fired subset versus 0.30 overall).

**3. An exact-query cache is the target, not a count or a coverage rule.** Every gate
degraded as the sample grew, each looking best at the size where it fired least. What has
held across every run is the diagnosis: the useful signal is "I have this query's own
stored results", and every rule here is an awkward proxy for it. A query-keyed cache
catches all 1,597 repeats rather than 794, cannot fire on the wrong query, and - because a
hash-keyed entry never enters the candidate pool for other queries - sidesteps the density
problem in the section above. That is the approach on branch `wiki-results-in-index`
(`b39e9f8`).

Suggested next step: build the exact-query cache and measure it head-to-head against
`term_coverage = 1.0` on the same repeat-weighted stream. The prediction from these numbers
is roughly double the savings with no ranking risk.

Do not enable `WIKI_INDEX_CACHE_ENABLED` on the strength of the 596-query numbers.
