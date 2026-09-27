# Pass-3 labels for the Combined Search serving pool

The Combined Search model is trained on the 849 LLM-labelled queries (`devdata/llm_relabel/`),
whose pool was built from standard search, Super Search, Google and Staan. What the model
actually ranks at serving time is different: the index candidates `CombinedLTRRanker`
retrieves, plus Staan's results. On the en-gb eval, 63% of the index results it puts in its
top ten are weak (pass-3 overall ≤ 3) while good Staan results sit at ranks 11–30, and most of
those index results were never in the training pool. These scripts label that pool.

1. `build_pool.py`: for each of the 849 queries, Staan's results from the Pass-2 pool plus the
   index's top 30 under the shipped model (`RemoteIndex` against api.mwmbl.org), marking
   which pairs `pass3_judgments.jsonl` already grades. Resumable.

   ```sh
   DJANGO_SETTINGS_MODULE=mwmbl.settings_dev PYTHONPATH=. \
       uv run python scripts/combined_ltr_labels/build_pool.py
   ```

   It needs a local Redis (Django's app startup takes a lock in the cache) but no database.
2. `make_batches.py`: blind Pass-3 batches of the unjudged pairs, about 250 candidates each,
   with the query's Pass-1 intent and two already-graded anchors per query.
3. Claude Haiku 4.5 subagents judge one batch each, writing
   `haiku_work/out_NN.txt` in the prompt's `id || relevance || ethos || overall` format.
4. `consolidate.py`: checks every id is graded exactly once and in range, writes
   `devdata/combined_ltr_labels/pass3_serving_pool.jsonl` (anchors flagged), and reports the
   anchors' drift from their original grades.

The en-gb eval queries (`devdata/combined_providers_eval/engb/rows-0.05.json`) don't overlap
the 849, so they stay a clean held-out test for a model trained on these labels.
