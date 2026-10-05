# Combined Search: a holistic side-by-side judge

2026-09-29 · follows `combined-ltr-objective.md` · `scripts/combined_ltr_labels/holistic_eval.py`,
judgments in `devdata/combined_ltr_labels/holistic_judgments.jsonl`

> **Where things are:** the scripts and data behind this write-up are on the archived
> branch `claude/blissful-maxwell-dq1ddl` (mwmbl/mwmbl#462), not in main. The ordering
> these experiments chose, Jev composite + Staan rank, is what Combined Search serves
> (`mwmbl/tinysearchengine/jev_rank.py`). To compare two rankings holistically, use
> `mwmbl/rankeval/evaluation/holistic_judge.py`; to build a Jev composite arm offline,
> `mwmbl/rankeval/evaluation/jev_order.py`.

**Question:** pass-3 NDCG grades each page alone, so it can't see a list's redundancy, which
intents it covers, its extracts, or whether its #1 answers the query. That is what MMR is for,
so NDCG can't evaluate it. Can a Haiku judge that compares two whole top-ten lists stand in,
and does it agree with what NDCG already tells us?

## Method

- **Side by side:** each comparison shows one query and two top-ten lists (title, URL,
  extract), and the judge says A, B or tie, how strongly (slight/clear/strong), and the main
  reason (top_result, relevance, junk, redundancy, coverage, extracts, ethos). It lists the
  best result and the weak results by position, with a reason each, first.
- **Ethos:** pass 3's ethos rubric word for word, with its rule that relevance comes first
  and the destination a navigational or transactional query wants wins even when commercial.
- **Extracts are judged, not hidden.** Each list shows the text it served: Brave's own
  descriptions, our extracts for index pages, Staan's snippets for Staan's results. Poor
  extracts count against a list, but after the result itself. Brave's `<strong>` markup is
  stripped.
- **Both orders:** each comparison is judged twice, A/B and B/A, by different Haiku 4.5
  subagents (878 judgments, 36 batches of 25). A verdict scores −3..3; the two are averaged.

Two validation sets on the en-gb queries, against `engb_staan_arms.json`:

- **brave:** Brave against `shipped`, every query where they differ (289).
- **ndcg:** per query, the two of our arms whose NDCG@10 differs most, where that's ≥ 0.05
  (150).

## Results

| | brave vs shipped | ndcg (widest arm pair) |
|---|---|---|
| Comparisons | 289 | 150 |
| Preference for NDCG's winner (−3..3) | +0.78 [+0.64, +0.92] | +0.67 [+0.46, +0.87] |
| NDCG's winner preferred / other / tie | 66% / 19% / 15% | 61% / 24% / 15% |
| Agrees with NDCG's sign, where it decides | 82% of 246 | 85% of 128 |
| Spearman (NDCG gap, preference) | 0.55 | 0.57 |
| Both orders agree on the direction | 65% | 67% |

Agreement with NDCG's sign by the size of its gap (brave set): 63% below 0.05, 81% at
0.05–0.10, 88% at 0.10–0.20, 98% above 0.20.

- **It passes both checks.** Brave wins clearly, and where NDCG sees a real gap the judge
  agrees 80–98% of the time, rising with the gap.
- **Position bias is small:** A is chosen in 53% of non-tie verdicts.
- **A single verdict is noisy.** 25% of comparisons get opposite verdicts in the two orders
  (73 of 289 and 30 of 150), almost all slight or clear, and 11% have one tie. Averaged over
  hundreds of queries the signal is strong, but a gap the size of the recent arm differences
  (NDCG ±0.004) needs many more queries or more judgments per comparison.
- **Main reasons:** junk, relevance and top result dominate (about 85%). Redundancy and
  coverage account for about 12% of the Brave verdicts and 17% of the arm-pair ones. The
  judge rarely gives extracts (8) or ethos (1) as the main reason, though it flags poor
  extracts on individual results.
- **Where it disagrees with NDCG, it often has a case:** a Facebook error page at #1 ("facebook
  uk"), login/cart utility pages ("pokemon center"), a scam site at #9 ("sign in hotmail"),
  the same BBC article from several years ("world book day"), and UK over US local results
  ("fish and chips near me"). Some are judgment calls, such as Wikipedia vs the official site
  at #1 for a brand query.

## MMR (2026-09-29)

The best learned arm served with MMR, `ndcg+new (en-gb Staan)`, and the same model without
MMR, each against the reference `staan-first, fill ndcg+new, no MMR`. Both are compared on every en-gb
query with complete grades (289 each), in both orders: 1,156 judgments, 48 batches
(`holistic_eval.py batches mmr`, judgments in `holistic_mmr_judgments.jsonl`). One judge
wrote `"better": "slight A"` for 12 verdicts that also gave strength `slight`; these were
normalised to `A`/`B`.

| | with MMR vs reference | without MMR vs reference |
|---|---|---|
| NDCG@10 gap (for reference) | −0.015 | +0.001 |
| Preference for the arm (−3..3) | −0.29 [−0.40, −0.17] | −0.22 [−0.35, −0.10] |
| Arm preferred / reference / tie | 29% / 53% / 18% | 32% / 46% / 21% |
| Both orders agree on the direction | 50% | 59% |
| Agrees with NDCG's sign, where it decides | 69% of 237 | 64% of 227 |
| Duplicate flags per judged list | 0.026 (reference 0.065) | 0.067 |

- **The reference wins both comparisons.** Staan-first, filled by `ndcg+new` without
  MMR, is preferred to the best MMR model by 0.29 on the −3..3 scale. NDCG put the gap at
  −0.015.
- **Without MMR the learned ordering still loses, by 0.22.** NDCG called it a tie
  (+0.001). The judge sees something NDCG doesn't in Staan's own order, mostly at the top:
  relevance and top result are the main reasons for about 60% of verdicts.
- **MMR itself makes little difference to the overall verdict.** Paired per query, with
  MMR minus without MMR (both against the reference) is −0.06 [−0.19, +0.06]. The small
  NDCG cost of MMR shows up with the same sign, but it isn't significant here.
- **MMR does what it's for.** Judges flag duplicates in 2.6% of the MMR lists' results,
  against 6.5–6.7% for the two lists without it. Redundancy is the main reason in 25
  verdicts with MMR and 51 without. But fewer duplicates don't outweigh what MMR demotes.
- **These are small differences.** The two orders agree on the direction only 50–59% of
  the time, less than in validation (65–67%), because the lists are close. The two
  headline gaps are clear of zero; the MMR-vs-no-MMR difference isn't.

## Next

- **MMR.** Done above: the reference beats both the MMR and the no-MMR learned ordering.
  A lighter or Staan-exempt kernel is only worth testing on top of Staan-first, where MMR
  would apply to the fill alone.
- **Noise.** With one pair of judgments per comparison, detecting a small gap needs many
  queries. Three or four judgments per comparison, or a stronger judge on the comparisons
  where the two orders disagree, would tighten it.
- **Prompt:** four judges gave `wrong-locale` as the main reason, which isn't in the list
  (it's recorded as `other`). Add `locale` as a main reason.

## Jev (2026-09-30)

Three Jev (TypeSafe) orderings of the same pool against the reference, on every en-gb query
where their lists differ, in both orders: 722 comparisons, 1,444 judgments, 58 batches
(`holistic_eval.py batches jev`, judgments in `holistic_jev_judgments.jsonl`). Setup and pass-3
numbers are in `combined-search-jev-eval.md`.

- **Pool:** ndcg+new's top 30 without MMR, plus Staan's ten, re-ranked fresh
  (`jev_pool.py`). The rebuilt reference matches `engb_staan_arms.json` on 264 of 295 queries.
  The index has drifted since, and every arm here fills from the same fresh pool.
- **Jev:** one pointwise UK-relevance Score per candidate. The arms:
  - `staan-first, fill Jev`: Staan's order, then Jev;
  - `Jev + Staan rank`: Jev's score (0–3) minus 0.05 × Staan's position (10 if absent);
  - `Jev re-rank`: Jev alone.

| | Jev + Staan rank | staan-first, fill Jev | Jev re-rank |
|---|---|---|---|
| Comparisons (lists differ) | 289 | 144 | 289 |
| Pass-3 NDCG@10 gap | +0.035 | +0.009 | +0.031 |
| Preference for the arm (−3..3) | **+0.31 [+0.18, +0.44]** | **+0.30 [+0.18, +0.42]** | +0.00 [−0.13, +0.14] |
| Arm preferred / reference / tie | 54% / 28% / 19% | 49% / 19% / 33% | 43% / 43% / 15% |
| Both orders agree on the direction | 60% | 52% | 61% |
| Agrees with NDCG's sign, where it decides | 71% of 235 | 66% of 97 | 61% of 246 |

- **`Jev + Staan rank` is the first ordering to beat the reference holistically.** The learned
  orderings in the MMR section lost to it by 0.22–0.29; this one wins by 0.31. Top result
  (158) and relevance (132) are the main reasons.
- **Jev alone ties the reference, though NDCG puts it +0.031 ahead.** It gives up Staan's top
  of the list: top result is the main reason in 215 of its 578 verdicts. That is this
  document's MMR finding again: Staan's order has value that per-page NDCG doesn't see.
- **Filling with Jev wins where it changes anything.** It differs from the reference on only
  144 queries (only positions below Staan's results move), and there it is preferred by
  0.30, mostly for dropping junk. Over all 289 queries that is about +0.15.
- **Caveats.** The Staan weight (0.05) was picked on this eval set's pass-3 NDCG, from five
  values. Judges for batches 00–03 first produced templated verdicts (22 ties in 25, three
  distinct notes); those were deleted and re-judged. One verdict's `"better": "slight A"` was
  normalised to `A`.

## Jev as a model feature (2026-09-30)

`Jev + Staan rank` hand-picks its Staan weight. Can the model learn the combination instead?
`jev_scores.py` scores all 51,561 LLM-labelled training pairs with Jev, and
`jev_feature_experiment.py` adds that score as a feature of ndcg+new.

**Cross-validated** over all 849 training queries (Jev has seen none of them), pass-3
NDCG@10 on the serving pool:

| Arm | NDCG@10 | vs ndcg+new, 95% CI |
|---|---|---|
| ndcg+new | 0.876 | — |
| + `jev`, every candidate | 0.900 | +0.023 [+0.019, +0.027] |
| + `jev`, stage-1 top 30 only (cascade) | 0.900 | +0.024 [+0.019, +0.028] |
| Jev alone | 0.856 | −0.020 [−0.028, −0.012] |
| Jev + Staan rank | 0.875 | −0.001 [−0.007, +0.005] |

That is three times the MiniLM feature's gain in the same setup, and the cascade keeps all
of it, so serving needs one Jev request per query.

**End to end on en-gb** (`jev_ltr_eval.py`): the cascade inside `CombinedLTRRanker`,
without MMR, with 136 top-30 candidates scored by live Jev calls. Pass-3 NDCG@10 over 289
queries: the cascade 0.853, the reference 0.828 (+0.026 [+0.019, +0.034]), and `Jev + Staan
rank` 0.863 (the cascade trails it by 0.010 [0.005, 0.014]).

**Holistic** (`holistic_eval.py batches jev-ltr`, 1,154 judgments in 48 batches,
`holistic_jev_ltr_judgments.jsonl`):

| | cascade vs reference | cascade vs Jev + Staan rank |
|---|---|---|
| Comparisons | 289 | 288 |
| Preference for the cascade (−3..3) | +0.02 [−0.12, +0.15] | **−0.26 [−0.36, −0.16]** |
| Cascade preferred / other / tie | 40% / 39% / 21% | 26% / 48% / 26% |
| Both orders agree on the direction | 55% | 46% |

- **The learned model ties the reference and loses to the hand rule.** It gains per-page
  NDCG, as every learned ordering in this document did. But like `Jev re-rank`, it overrides
  Staan's top results, and the judge doesn't reward that. Relevance (168) and the top result
  (144) are the main reasons in its comparison with the reference.
- **So `Jev + Staan rank` remains the best ordering.** A model trained on pass-3 labels
  learns what pass-3 rewards, and pass-3 grades pages one at a time.
- **Data cleaning.** One verdict gave `"better": "slight"` with a note naming B, and was
  set to B. Three ties lacked a `reason` and got `none`. No batch looked templated.

## Jev + Staan rank, tuned on the training queries, and against Brave (2026-10-01)

`Jev + Staan rank` had its Staan weight (0.05) picked on these en-gb queries' pass-3 NDCG.
Picked instead on the 849 training queries' serving pool, the weight peaks at 0.15 (NDCG@10
0.885, against 0.875 at 0.05). The original labelled pool peaks at 0.05–0.1. On en-gb,
pass-3 NDCG gives 0.858 at w=0.15 and 0.863 at w=0.05: the eval-set choice was slightly
optimistic. `holistic_eval.py batches jev-tuned` and `jev-brave`, 578 judgments each:

| | w=0.15 vs reference | w=0.15 vs Brave |
|---|---|---|
| Comparisons | 289 | 289 |
| Preference for w=0.15 (−3..3) | **+0.49 [+0.37, +0.61]** | **−0.25 [−0.38, −0.12]** |
| w=0.15 preferred / other / tie | 60% / 21% / 19% | 33% / 47% / 20% |
| Both orders agree on the direction | 62% | 49% |

- **Tuning on separate queries made it better, not worse.** It beats the reference by +0.49,
  against +0.31 for the eval-tuned w=0.05 in the `jev` experiment. Leaning harder on
  Staan's order is what the judge rewards.
- **Brave is still ahead, but closer.** In validation Brave beat `shipped` by +0.78; it
  beats this arm by 0.25.

**Where Brave wins** (the 300 verdicts preferring Brave):

- **Main reasons:** relevance 80, junk 63, top result 57, redundancy 39, coverage 38,
  extracts 21.
- **Weak-result flags on our list vs Brave's**, in those verdicts:

  | Flag | Ours | Brave's |
  |---|---|---|
  | off-topic | 181 | 50 |
  | poor extract | 111 | 27 |
  | thin | 67 | 28 |
  | duplicate | 65 | 16 |
  | wrong locale | 13 | 4 |

- **Mwmbl's index results are the weak link.** Our top ten averages 8.3 Staan results and
  1.7 from the index, yet 253 of the 460 flagged results we could place came from the index
  and 207 from Staan. Per result, an index page is about six times as likely as a Staan page
  to be flagged.
- **Recurring failures:**
  - **The wrong entity of the same name:** other hotels for "bankside hotel", other
    practices for "muirhead medical practice", the wrong Usman Tariq.
  - **The wrong sense:** baking and biology for "slaters" (the menswear retailer);
    definitions of bullion for "bullion by post".
  - **Off-topic spill from the index:** Fortnite cosmetics for "codes for goalbound";
    Arrival and Tenet for "ending of sirens explained"; hair-transplant spam for "capital
    hair and beauty".
  - **Repeated pages from one site:** "makerere university", "byfords holt".

## Targeting the Brave losses: composite Jev questions, an index penalty, a site cap (2026-10-01)

On the results the Brave comparison flagged, Jev's relevance score hardly separates them from the
rest: index results flagged off-topic or otherwise average 1.84 out of 3, clean ones 2.07. So
`jev_composite.py` asks Jev three Score questions per candidate, one request per query:
relevance (as before), **entity** (about the entity or sense the query means? 0–2) and
**quality** (substantive, or thin, spam or junk? 0–2). The arm scores relevance + α·entity +
β·quality − w·Staan position − p·(not from Staan). The weights are tuned on the 849 training
queries' pass-3 NDCG@10, never on en-gb.

- **The entity question doesn't help.** Every good weighting gives it weight 0. On the flagged
  results it separates no better than relevance does: for "bankside hotel" it rates other
  hotels 1.8 of 2.
- **Quality and the index penalty do.** The tuned arm is quality 0.5, Staan 0.1, index 0.5:
  NDCG@10 0.889, against 0.885 for `w=0.15`, +0.004 [+0.002, +0.007]. Without the quality
  question it is 0.887, and without the index penalty 0.880. The surface is flat, so the
  weights were picked from the middle of the plateau, not its edge.
- **Site cap:** after ranking, a site's third and later results move below the top ten.

Holistic judgments (`batches jev-composite`, `jev-cap`, `jev-composite-brave`):

| | composite vs w=0.15 | composite + cap vs composite | composite vs Brave |
|---|---|---|---|
| Comparisons | 284 | 71 | 295 |
| Preference for the first arm (−3..3) | **+0.16 [+0.07, +0.25]** | **−0.65 [−0.87, −0.42]** | −0.12 [−0.26, +0.02] |
| First preferred / second / tie | 44% / 29% / 27% | 13% / 65% / 23% | 37% / 42% / 22% |
| Both orders agree on the direction | 46% | 59% | 55% |

- **The composite beats `w=0.15`.** Relevance (179) and the top result (80) are the main
  reasons. It differs on 284 of 295 queries, but mostly in the bottom half, so a third of
  verdicts are ties or opposite in the two orders.
- **The site cap loses badly.** The pages it pulls in to replace a site's third and later
  results are worse than the repeats: relevance (61) and junk (29) are the main reasons. Often
  the repeats are what the searcher wants (the official site's own pages). 19 queries keep
  three or more from one site because the pool has nothing else.
- **Brave's lead is no longer significant.** On the 289 queries both Brave runs share, the
  preference moves from −0.25 [−0.38, −0.12] to −0.11 [−0.25, +0.03], paired +0.14
  [+0.01, +0.27].
- **Weak-result flags, ours relative to Brave's** in the same verdicts: these judges flag more
  on both sides than the last run's, so ratios compare better than counts. Poor extract falls
  from 2.0× Brave's to 1.8×, off-topic from 1.9× to 1.7×, duplicate from 1.2× to 0.8×. Thin
  stays at 1.3×, and wrong locale rises from 0.5× to 1.0×.
- **Caveats.** The six queries new to the Brave comparison have ungraded pages, so they have
  no NDCG gap. A few judges wrote `organization` or another reason outside the list (recorded
  as `other`). One judge used a script to parse its batch but wrote its verdicts by hand. No
  batch looked templated: every batch has a distinct note per verdict.

**Next:** the remaining gap is relevance, junk and coverage. Off-topic results are still 1.7×
Brave's. A better entity signal than a Jev Score question is needed, for example the query's
Wikidata entity or a pairwise Jev choice between candidates for the same name. Wrong locale
rose and is worth a targeted locale question.
