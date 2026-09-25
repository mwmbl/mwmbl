# Combined Search Haiku judging pipeline

One-off scripts behind `mwmbl/rankeval/combined-search-haiku-eval.md`. They are kept for
provenance, not polished: the results are reproduced from the committed data by
`python -m mwmbl.rankeval.evaluation.haiku_arms_report`, which needs none of this.

Scratch files (batches, manifests, raw judge outputs) go in `$HAIKU_WORK_DIR`, by default
`devdata/combined_providers_eval/haiku_work/` (gitignored). The consolidated judgments are
in `devdata/combined_providers_eval/haiku/`.

## en-us pass (relevance only, no locale)

1. `build_pool.py OUT.json`: title and extract for every URL in the arms of
   `rows-0.05.json`, from the Staan external cache and the Brave joblib cache. Run with
   `STAAN_SEARCH_API_KEY` unset, so a cache miss can never become a paid call.
2. `make_batches.py`: 15 blind, shuffled batches. The judges were Claude Haiku 4.5
   subagents. List-style output was misaligned for about a quarter of the queries, which
   were re-graded with keyed output (`{"qid": n, "grades": {"0": g, ...}}`).

## en-gb run (both engines asked for UK results)

1. `settings_engb.py` + `run_engb.py`: the combined-providers eval with Staan
   `market=en-gb` and Brave `country=GB&search_lang=en`. The external cache is keyed by query
   alone, so Staan gets a cache file of its own; Brave gets a joblib cache of its own for
   the same reason. It also records each arm's text into `engb/pool_text.json`.

   ```sh
   set -a && source .env && set +a
   PYTHONPATH=scripts/combined_search_haiku:. DJANGO_SETTINGS_MODULE=settings_engb \
       DATABASE_URL="postgres://daoud@" .venv/bin/python scripts/combined_search_haiku/run_engb.py --fraction 0.05
   ```

   The Brave key ran out of quota (HTTP 402) on the last 3 of 298 queries, so the rows are
   the 295 with a Brave result.
2. `make_batches_uk.py`: UK-relevance batches (keyed output).
3. `wiki_pool.py` and `deep_pool.py` (same settings as above): Wikipedia candidates and the
   combined LTR ranking to depth 30, both with MiniLM scores. `deep_pool.py` also times the
   LTR and MiniLM stages.
4. `deep_eval.py dump N`: batches for URLs any re-ranking arm puts in its top ten that are
   not yet graded.
5. `make_batches_p3.py`: every graded URL again, with Mwmbl's pass-3 prompt
   (`scripts/llm_relabel_pass3_judge.py`), one line per candidate, `id || rel || ethos || overall`.

`filter_count.py` measures how many retrieved candidates the LTR's majority-terms filter
drops.
