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

**3. `from_wiki_only` is the one to pursue.** Counting only documents a previous call
stored makes it an approximate exact-query cache rather than an inference, and it behaves
like one: ~50% of calls avoided on repeat-weighted traffic for −0.0117 NDCG on repeats,
with the cost essentially flat as the savings grow.

It is still only *approximate* — it fires on term overlap too, and on the gold set those
24 firings cost −0.2872 on the fired subset. The exact version keys on the query itself and
so cannot fire on overlap at all: that is the query-hash cache term on branch
`wiki-results-in-index` (`b39e9f8`), which by construction cannot produce those losses.

Suggested next step: evaluate the exact-query cache against `from_wiki_only` on the
repeat-weighted stream. The prediction from these numbers is that it captures the same
~50% saving with the residual −0.0117 removed. If that holds, ship the exact cache and
drop the count-based gate entirely.
