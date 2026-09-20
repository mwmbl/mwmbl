# Rank evaluation

Tools for measuring Mwmbl's ranking quality against a gold standard.

## The gold dataset

The gold rankings come from **Firefox-extension search scrapes**: volunteers
running the [Mwmbl extension](https://addons.mozilla.org/firefox/addon/mwmbl-web-crawler/)
submit the results they are shown by commercial search engines. The server
stores each submission in the Backblaze bucket (`mwmbl-eu-crawl`) under
`1/<VERSION>/<date>/dataset/<user-hash>/<file>.json.gz`.

`mwmbl/rankeval/dataset/extension_dataset.py` downloads those files into
`scripts/downloads/` and flattens them into the train/test CSVs under
`devdata/rankeval-2026-04/remote-datasets/` (`rankings-train.csv`,
`rankings-test.csv`), which is what the evaluation scores against.

### Creating / refreshing the dataset

Downloading needs Backblaze credentials — `MWMBL_KEY_ID` and
`MWMBL_APPLICATION_KEY` — in the environment or a repo-root `.env` file.

```bash
# Pull any new scrapes from Backblaze, then (re)build the CSVs.
# Already-downloaded files are skipped, so this is incremental.
uv run python -m mwmbl.rankeval.dataset.extension_dataset

# Rebuild the CSVs from files already in scripts/downloads/, without network.
uv run python -m mwmbl.rankeval.dataset.extension_dataset --no-download
```

## Running an evaluation

`mwmbl/rankeval/evaluation/evaluate.py` scores any `RankingModel`
(`.predict(query) -> list[url]`) against the gold set, reporting NDCG and the
proportion of gold URLs matched.

- **Standard search** — `mwmbl/rankeval/evaluation/evaluate_remote.py` evaluates
  the production ranker (`LTRRanker` + MMR) over a `RemoteIndex`
  (`https://api.mwmbl.org`).

```bash
uv run python -m mwmbl.rankeval.evaluation.evaluate_remote
```

(A `RankingModel` wrapper around the Super Search pipeline, for comparing Super
Search v2 against standard search, is added separately.)

## Combined Search

`GET /api/v2/combined-search/` pools the Mwmbl index, Staan and Wikipedia and
ranks the union with its own LTR model (`mwmbl/resources/model-combined.xgb`,
falling back to the deployed `model.xgb` until that artifact exists). It is
intended to replace Super Search, which repeated evaluation has found does not
beat standard search — see `SUPER_SEARCH_EVAL_FINDINGS.md` and
`SUPER_SEARCH_ADD_SOURCES_FINDINGS.md`, where the conclusion is that the
re-ranker, not the source set, is the bottleneck.

```bash
# The endpoint's ranking core against the gold set, and the no-Staan ablation.
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.evaluate_combined_search --fraction 0.05
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.evaluate_combined_search --fraction 0.05 --no-staan

# Standard search, Super Search and both Combined Search arms on the same queries.
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.compare_search_modes --fraction 0.05
```

### Training the combined model

The combined model is the same 50-feature Rust XGBoost model as standard search,
retrained on a candidate pool that includes Staan. A provider's own ranking
reaches it only through the `score` column (the `item_score` feature), exactly as
Wikipedia's does — there is no source feature — so the constant used to build the
training data has to be the constant the serving path uses. See `STAAN_TOP_SCORE`
in `mwmbl/tinysearchengine/staan.py`.

```bash
# 1. Add Staan's results to the existing Pass-2 pool (resumable; merge is idempotent).
DATABASE_URL="postgres://daoud@" uv run python scripts/llm_relabel_pass2_augment_staan.py
DATABASE_URL="postgres://daoud@" uv run python scripts/llm_relabel_pass2_augment_staan.py --merge

# 2. Judge only the pairs Staan added — Pass 3 is append-only and keyed by (query, url).
uv run python scripts/llm_relabel_pass3_judge.py --status
uv run python scripts/llm_relabel_pass3_judge.py --dump-batch 200 --tag staan1 > batch.txt
uv run python scripts/llm_relabel_pass3_judge.py --merge judge_out.txt --tag staan1
uv run python scripts/llm_relabel_pass3_judge.py --calibration   # what Staan is worth, blind

# 3. Rebuild learning-to-rank-llm.csv.gz with the `staan` pool in it.
uv run python scripts/llm_relabel_build_dataset.py

# 4. Train and gate. Keep --split-seed fixed across arms.
DATABASE_URL="postgres://daoud@" uv run python -m mwmbl.rankeval.ltr.llm_experiment \
    --mode baseline --model-path mwmbl/resources/model.xgb --note deployed
DATABASE_URL="postgres://daoud@" uv run python -m mwmbl.rankeval.ltr.llm_experiment \
    --mode mixed --ext-weight 0.25 --overall-threshold 4 --scale-pos-weight 1.0 \
    --add-curation --curation-weight 0.5 --note combined \
    --save-model devdata/rankeval-2026-04/model-combined.xgb
```

`llm_experiment` reports two axes and **both** gate a retrain: NDCG on a held-out
split of the Haiku grades, and pair accuracy on a held-out split of the human
curation pairs. The deployed model set the precedent at NDCG@10 0.744 / human
pair-accuracy 0.818; a regression on the human axis is a blocker even when the
Haiku axis improves.
