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

## Next

- **MMR.** The ndcg set has only 3 pure MMR/no-MMR pairs, so it says nothing about MMR yet.
  Run `ndcg+new (en-gb Staan)` with and without MMR, and a lighter or Staan-exempt kernel,
  each against Staan-first, fill `ndcg+new`, no MMR.
- **Noise.** With one pair of judgments per comparison, detecting a small gap needs many
  queries. Three or four judgments per comparison, or a stronger judge on the comparisons
  where the two orders disagree, would tighten it.
- **Prompt:** four judges gave `wrong-locale` as the main reason, which isn't in the list
  (it's recorded as `other`). Add `locale` as a main reason.
