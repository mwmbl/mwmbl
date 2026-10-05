# Combined Search vs Brave: Haiku-judged evaluation

2026-09-24 · follows `combined-search-quality-handover.md` (same directory) · every table
below is printed by `python -m mwmbl.rankeval.evaluation.haiku_arms_report`, from data
committed under `devdata/combined_providers_eval/`.

**Goal:** get Combined Search (Staan + Mwmbl) to beat Brave's API on quality for an LLM chat
client who also cares about Mwmbl's values.

## Summary

- **Brave's lead is real under every judge we tried**, and it isn't a locale artefact:
  0.065–0.11 NDCG@10 behind Brave, depending on the judge, even with both engines asked for UK
  results.
- **No re-ranking of our current candidates can close it.** Brave's lead comes from
  candidates, not order: the pages Brave finds that we don't are graded good 84% of the time,
  against 65% for the pages only we find.
- **Staan-first wins on every measure.** Keep Staan's results in Staan's order and fill the
  remaining slots from our other candidates. The best version so far fills them with MiniLM
  over the LTR's top 30 plus Wikipedia: +0.04–0.05 over shipped, and level with Brave on
  ethos.
- **A full MiniLM re-rank loses** on relevance, on pass-3 overall and on ethos, at any depth,
  with or without Wikipedia. Finding 1 of the previous handover said it "hurts gold"; the
  accurate verdict is that it never helps.
- **Brave does not lose on ethos.** Mwmbl index results are the lowest-ethos source we add;
  Wikipedia is the highest, but its search results are mostly off-topic.

## Method

- **Queries:** the 298-query sample of the gold test set used in the earlier handovers
  (`rows-0.05.json`). The queries come from UK Google autocomplete.
- **Judges:** Claude Haiku 4.5, blind (the engine is never shown), candidates shuffled,
  judging title + URL + snippet. The prompts are in `devdata/combined_providers_eval/haiku/prompts/`:
  - `relevance_enus`: relevance 0–3, no locale.
  - `relevance_engb`: relevance 0–3 for a UK searcher; a result for another country is
    graded at most 1 where the answer depends on country.
  - `pass3_engb`: Mwmbl's own pass-3 prompt (`scripts/llm_relabel_pass3_judge.py`):
    relevance 0–3, ethos 0–3, overall 0–10. No Pass-1 intents exist for these queries, so the
    judge infers the intent.
- **Metric:** NDCG@10 against the ideal ordering of every judged URL for the query (gains
  2^rel − 1 for relevance, 0–10 for overall), with bootstrap 95% intervals over queries.
  Ethos doesn't depend on the query, so it is reported as a position-weighted mean over the
  top ten, plus the share of the top ten with ethos ≤ 1.
- **Two runs:**
  - `en-us`: `rows-0.05.json`, as shipped. Staan used `market=en-us`, and Brave was sent no
    country, so it defaulted to the US. Only about 9% of either engine's results were `.uk`.
  - `en-gb`: `engb/rows-0.05.json`, re-run with Staan `en-gb` and Brave `country=GB`.
    About a third of both engines' results are `.uk`. It has 295 queries: the Brave key's
    quota ran out on the last three.

## Results

### en-us run, relevance judge (295 queries)

| Arm | Haiku NDCG@10 | vs shipped | vs Brave |
|---|---|---|---|
| combined (shipped) | 0.747 | — | −0.112 [−0.131, −0.094] |
| staan | 0.762 | +0.015 [+0.003, +0.026] | −0.098 |
| staan-first + fill | 0.789 | **+0.042 [+0.032, +0.051]** | −0.070 |
| MiniLM re-sort of top 10 | 0.743 | −0.004 [−0.011, +0.004] | −0.116 |
| MiniLM re-sort of combined ∪ Staan | 0.775 | +0.028 [+0.017, +0.038] | −0.084 |
| brave | 0.859 | +0.112 | — |

On gold, with URLs normalised (tracking parameters such as Staan's `srsltid` were stopping
exact matches), Staan and Staan-first are level with Brave: −0.017 [−0.039, +0.005].

### en-gb run, UK-relevance judge (295 queries)

| Arm | Haiku NDCG@10 | vs Staan-first | vs Brave |
|---|---|---|---|
| combined (shipped) | 0.732 | −0.037 | −0.117 |
| staan | 0.746 | −0.022 | −0.103 |
| staan-first + fill | 0.768 | — | −0.081 [−0.096, −0.065] |
| MiniLM re-rank, LTR top 10 / 20 / 30 | 0.714 / 0.721 / 0.721 | −0.047 to −0.054 | −0.13 |
| … + Wikipedia | 0.722 / 0.726 / 0.723 | −0.043 to −0.046 | −0.12 to −0.13 |
| **staan-first, fill MiniLM(LTR top 30 + Wikipedia)** | **0.785** | **+0.016 [+0.013, +0.020]** | −0.064 |
| brave | 0.849 | +0.081 | — |

### en-gb run, pass-3 judge (287 queries)

| Arm | Overall NDCG | Relevance NDCG | Ethos | Ethos ≤ 1 | Overall vs Staan-first | Ethos vs Brave |
|---|---|---|---|---|---|---|
| combined (shipped) | 0.801 | 0.781 | 1.73 | 45% | −0.029 | −0.077 [−0.103, −0.051] |
| staan | 0.799 | 0.787 | 1.78 | 42% | −0.031 | −0.027 |
| staan-first + fill | 0.830 | 0.813 | 1.75 | 44% | — | −0.057 |
| MiniLM re-rank, LTR top 10 / 20 / 30 | 0.789 / 0.783 / 0.779 | ~0.76–0.78 | 1.67–1.70 | 44–45% | −0.040 to −0.051 | −0.11 to −0.13 |
| … + Wikipedia | 0.796 / 0.786 / 0.781 | ~0.76–0.78 | 1.72–1.74 | 42–43% | −0.033 to −0.048 | −0.06 to −0.09 |
| **staan-first, fill MiniLM(LTR top 30 + Wikipedia)** | **0.841** | **0.822** | **1.78** | 42% | **+0.012 [+0.008, +0.015]** | −0.024 [−0.048, +0.000] |
| brave | 0.895 | 0.885 | 1.80 | 39% | +0.065 | — |

**Pass-3 judgments by where the URL came from:**

| Source | URLs | Relevance | Ethos | Overall | Ethos ≤ 1 |
|---|---|---|---|---|---|
| Staan | 2,544 | 2.55 | 1.71 | 6.68 | 43% |
| Brave only | 1,501 | 2.50 | 1.65 | 6.53 | 43% |
| Mwmbl index | 1,225 | 1.33 | 1.58 | 3.42 | 48% |
| Wikipedia | 225 | 1.12 | 2.81 | 3.69 | 4% |

## Findings

1. **The gap is candidates, not ordering.** (These figures come from the session's scratch
   analysis, not the report script.) On the `en-us` pool, Staan ∪ shipped (about 15
   URLs a query) sorted perfectly by Haiku's grades scores 0.854, about equal to Brave's
   actual 0.859. On `en-gb` it scores 0.843, against Brave's 0.874.
2. **Staan's failure modes**, from the queries Brave wins by most:
   - **Phrases and entities:** for "dancing plague", Staan returned pages about dancing
     (Strictly, dance and health studies); Brave returned the Dancing plague of 1518.
   - **Authority:** for "cupcakes", Staan returned small baking blogs; Brave returned BBC Good
     Food, BBC Food and the Hummingbird Bakery.
   - **Partial locale:** for "price of gold today", Staan under `en-gb` still returned US
     sites quoting prices in USD.
3. **Locale matters for the product, but not for the comparison.** Both engines improve under
   `en-gb`, and Brave's lead holds. Gold falls for both under `en-gb` (Brave 0.780 → 0.743
   normalised), which suggests the gold SERPs were scraped with US Google settings despite the
   UK queries. Don't use gold to judge UK quality.
4. **MiniLM helps only when it fills gaps.** Everything that lets MiniLM override Staan's
   order loses. The judge was trained on pass-3 overall, but that doesn't carry through:
   re-ranking the LTR list without Wikipedia gives the lowest ethos of any arm.
5. **Mwmbl's own index lowers both relevance and ethos.** The LTR retrieves a median of 134
   candidates, and its majority-terms filter (`predictions > 0.0`, `ltr_rank.py`) keeps a
   median of 33, so about 75% are dropped. The index candidates that survive are still weak.
6. **Wikipedia is the one high-ethos source, but it needs a relevance gate.** Letting MiniLM
   choose which Wikipedia results fill the gaps is what brings the best arm level with Brave
   on ethos.

## Staan API notes

From `docs.staan.ai/api/search`, tested live:
- `count` must be 10; anything else is a 400 error.
- `offset` pages through results, at the cost of another call.
- `market` accepts `en-gb` and similar; `STAAN_MARKET` defaults to `en-us`.
- `extra_snippets=true` returns three query-reranked chunks per result, with no visible
  latency cost.
- `full_content=markdown` is patchy: gov.uk returned only its 121-character survey banner.
- `include_domains` / `exclude_domains` are POST only.
- Staan's order is not deterministic across calls.

## Next steps

1. **Ship Staan-first, filled by MiniLM over the LTR's top 30 plus Wikipedia.** MiniLM costs
   about 0.34 s of CPU for about 38 candidates on the dev machine. Take Staan's market from
   the request.
2. **Find relevant high-ethos candidates:** better Wikipedia and Wikidata matching; work out
   why index candidates score low on ethos, which is a crawl and curation question.
3. **Retrain the judge on today's labels** (about 11k Haiku judgments, keeping a held-out
   split), then test it as the fill ranker and as an ethos-aware LTR feature.
4. **Take the failure modes to Staan** (phrases, authority, locale). They sit in Staan's
   ranker, and they are most of Brave's remaining lead.
5. **Measure at the answer level** with `../web-search-api-evals`. Its `mwmbl_search` sampler
   calls the index-only `/api/v2/search/`, not Combined Search, so it needs a new sampler.
   Try `extra_snippets` there too.

## Data

In `devdata/combined_providers_eval/`:
- `rows-0.05.json`: the `en-us` run. `pool_text_enus.json` holds its result texts,
  rebuilt from the caches.
- `engb/rows-0.05.json`: the `en-gb` run, 295 queries. Alongside it:
  - `pool_text.json`: the arms' result texts;
  - `wiki_pool.json`: Wikipedia candidates, with MiniLM scores;
  - `combined_top30.json`: the LTR's top 30, with MiniLM scores.
- `haiku/relevance_enus.jsonl` (4,731 judgments), `haiku/relevance_engb.jsonl` (5,525) and
  `haiku/pass3_engb.jsonl` (5,495), each keyed by `query` and `url`.
- `haiku/prompts/`: the three judge prompts.

How the data was generated is described in `scripts/combined_search_haiku/README.md`.
