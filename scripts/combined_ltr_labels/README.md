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

## en-gb end to end with MiniLM features

`engb_minilm_eval.py` runs `engb_eval.py`'s pipeline for `shipped`, `ndcg+new` and
`ndcg+new` with each judge set as features, trained on all 849 queries plus the new
labels. It writes `engb_minilm_arms.json` and `pass3_engb_minilm.jsonl`, and
`engb_eval.judged()` reads every run's judgments, so each run batches only URLs no run
has judged. The results are in `mwmbl/rankeval/combined-ltr-objective.md`.

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py rank         # about 30 minutes
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py batches
# One Claude Haiku 4.5 subagent per engb_minilm_work/batch_NN.txt, writing out_NN.txt
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py consolidate
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_minilm_eval.py report
```

## MiniLM on the top K only

`minilm_cascade_experiment.py` runs the MiniLM feature as a cascade: the judge scores only
the stage-1 top 10, 20 or 30. It reads `minilm_scores.json` and writes
`minilm_cascade_experiment.json`. The results are in
`mwmbl/rankeval/combined-ltr-objective.md`.

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_cascade_experiment.py   # about 35 minutes
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_cascade_experiment.py staan   # Staan-monotone
PYTHONPATH=. uv run python scripts/combined_ltr_labels/minilm_cascade_experiment.py compare staan
```

`engb_cascade_eval.py` runs the cascade and the Staan-first fills end to end on en-gb, the
same way as `engb_minilm_eval.py`.

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_cascade_eval.py rank   # about 40 minutes
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_cascade_eval.py batches
# One Claude Haiku 4.5 subagent per engb_cascade_work/batch_NN.txt, writing out_NN.txt
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_cascade_eval.py consolidate
PYTHONPATH=. uv run python scripts/combined_ltr_labels/engb_cascade_eval.py report
```

- **Environment (this machine):**
  - The judges must be at `devdata/judge_train/models/minilm-{both,pointwise,pairs}-v1/onnx`
    (gitignored; locally a symlink to `../mwmbl/devdata/judge_train/models`), and
    `minilm_scores.json` built by `minilm_scores.py`.
  - Redis must be running.
  - Use `.venv/bin/python` with `PYTHONPATH=.` and
    `DJANGO_SETTINGS_MODULE=mwmbl.settings_dev`.
  - The "Failed to schedule background tasks" traceback at startup is harmless: there's no
    database.

## Holistic evaluation

`holistic_eval.py` has a Claude Haiku 4.5 judge compare two whole top-ten lists side by side,
in both orders, for an overall verdict with reasons (top result, relevance, junk, redundancy,
coverage, extracts, ethos). It is validated against Brave and against NDCG on en-gb. The
results are in `mwmbl/rankeval/combined-holistic-eval.md`.

```sh
PYTHONPATH=. uv run python scripts/combined_ltr_labels/holistic_eval.py batches
# One Claude Haiku 4.5 subagent per holistic_work/batch_NN.txt, writing out_NN.txt
PYTHONPATH=. uv run python scripts/combined_ltr_labels/holistic_eval.py consolidate
PYTHONPATH=. uv run python scripts/combined_ltr_labels/holistic_eval.py report
```
