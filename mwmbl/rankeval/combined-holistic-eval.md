# Combined Search: a holistic side-by-side judge

2026-09-29 · follows `combined-ltr-objective.md` · `scripts/combined_ltr_labels/holistic_eval.py`,
judgments in `devdata/combined_ltr_labels/holistic_judgments.jsonl`

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
