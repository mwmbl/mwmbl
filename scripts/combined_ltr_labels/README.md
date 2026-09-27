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

## Result (2026-09-27)

- **Pool:** 849 queries, 25,007 pairs. 16,925 were already graded. Nearly all of the 8,082
  new pairs are index results; Staan's were all graded already.
- **Judging:** 39 batches plus an 8-candidate top-up for ids the first pass skipped, each
  judged by one Claude Haiku 4.5 subagent. Written to `pass3_serving_pool.jsonl`: 8,082 new
  rows and 1,186 anchors.
- **The new pairs are weak, as expected:** mean overall 2.49. 73% score ≤ 3 and 27% have
  relevance ≥ 2. 766 (9%) score ≥ 7, and those are the index results the model should learn
  to promote.
- **Drift:** across all anchors it is small (overall +0.08, relevance +0.18, ethos −0.12),
  with 87% of relevance grades within one of the original. But agreement on the 0–10
  overall grade is loose: 47% within one, Spearman 0.33 (the anchors are mostly good Staan
  pages, so their range is narrow). Each judge is offset differently: mean overall drift
  ranges from −1.74 to +1.90 across judges (sd 0.70). All of a query's new pairs share one
  judge, so their order within the query is consistent, but setting them beside the query's
  original grades should first remove that judge's offset, using its anchors (`judge` in
  each row).

## Objective experiment

`objective_experiment.py` (cross-validation) and `engb_eval.py` (end to end on en-gb,
Haiku-judged) compare `binary:logistic`, `rank:ndcg` and `rank:pairwise`. The results are in
`mwmbl/rankeval/combined-ltr-objective.md`.

## MiniLM judges as features

`minilm_scores.py` scores every LLM-dataset row and serving-pool pair with the three
fine-tuned MiniLM judges (`devdata/judge_train/models/minilm-{both,pointwise,pairs}-v1`,
gitignored, so they have to be supplied). It caches the scores in `minilm_scores.json`,
which is 13 MB and uncommitted, so rerun it to reproduce. `minilm_experiment.py`
cross-validates `ndcg+new` with those features on the 424 queries no judge saw. The
results are in `mwmbl/rankeval/combined-ltr-objective.md`.

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_scores.py      # about 35 minutes on CPU
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_experiment.py
```

## Handover: en-gb end to end with MiniLM features

The cross-validation says adding the MiniLM judges to `ndcg+new` gains +0.006 to +0.010
NDCG@10. The next step is `engb_eval.py`'s end-to-end run on the 289 held-out en-gb
queries, with Haiku judging the top-ten URLs that no judgment covers yet.

- **Models.** Put the judges at `devdata/judge_train/models/minilm-{both,pointwise,pairs}-v1/onnx`
  (gitignored; locally a symlink to `../mwmbl/devdata/judge_train/models`). Then run
  `minilm_scores.py` to rebuild `minilm_scores.json`, which is gitignored.
- **Arms.**
  - Train on all 849 queries plus the new labels, with extension rows at weight 0.25.
    That is the `all` setting, the best in CV.
  - Suggested arms: `shipped`, `ndcg+new` (no MiniLM), `ndcg+new+pointwise`,
    `ndcg+new+both` and `ndcg+new+all3`.
  - Build the training features with `minilm_experiment.minilm_columns` and
    `objective_experiment.features`. The extension rows' MiniLM columns stay NaN.
- **Serving the features.** `LTRRanker` passes the model records with `query`, `title` and
  `extract`. So subclass `engb_eval.BoosterModel`: score each record with
  `Judge(model_dir).score(query, [doc_text(title, extract)])` for each judge, append those
  columns in `minilm_scores.MINILM_MODELS` order, then apply the same sigmoid and
  filter-as-exclusion.
  - Each candidate the ranker scores costs about 1/76 s per judge on CPU.
  - Retrieval is live against api.mwmbl.org, so rerun `shipped` and `ndcg+new` in the same
    pass. Arms are only comparable within one retrieval.
- **Don't overwrite the previous run.**
  - `engb_eval.py` hardcodes `ARMS` (`engb_arms.json`), `NEW_JUDGMENTS`
    (`pass3_engb_arms.jsonl`) and `WORK`. Write the MiniLM run to new files, e.g.
    `engb_minilm_arms.json` and `pass3_engb_minilm.jsonl`.
  - Make `judged()` read all three judgment files, so only URLs that are genuinely new
    get batched.
- **Judging.** `batches` → Claude Haiku 4.5 subagents, one per batch, each writing
  `out_NN.txt` → `consolidate` → `report`. Check the anchors' drift as in the objective
  run.
- **Leakage.** None of the en-gb queries are among the 849. Two of them ("bitcoin price",
  "microsoft teams") appear in the curation pairs export, which the judges' pairs task may
  have trained on. That's negligible, but they can be dropped from the report to be strict.
- **Environment (this machine):**
  - Redis must be running.
  - Use `.venv/bin/python` with `PYTHONPATH=.` and
    `DJANGO_SETTINGS_MODULE=mwmbl.settings_dev`.
  - The "Failed to schedule background tasks" traceback at startup is harmless: there's no
    database.
