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

# 4. Gate: the same config and split, trained WITHOUT then WITH the new source.
DATABASE_URL="postgres://daoud@" uv run python -m mwmbl.rankeval.ltr.llm_experiment \
    --mode mixed --ext-weight 0.25 --overall-threshold 4 --scale-pos-weight 1.0 \
    --add-curation --curation-weight 0.5 --exclude-pool staan --note no-staan
DATABASE_URL="postgres://daoud@" uv run python -m mwmbl.rankeval.ltr.llm_experiment \
    --mode mixed --ext-weight 0.25 --overall-threshold 4 --scale-pos-weight 1.0 \
    --add-curation --curation-weight 0.5 --note combined

# 5. Once a config wins, retrain it on everything and install it.
DATABASE_URL="postgres://daoud@" uv run python -m mwmbl.rankeval.ltr.llm_experiment \
    --mode mixed --ext-weight 0.25 --overall-threshold 4 --scale-pos-weight 1.0 \
    --add-curation --curation-weight 0.5 --test-size 0 --note combined-final \
    --save-model devdata/rankeval-2026-04/model-combined.xgb
cp devdata/rankeval-2026-04/model-combined.xgb mwmbl/resources/model-combined.xgb
```

`llm_experiment` reports two axes and **both** gate a retrain: NDCG on a held-out
split of the Haiku grades, and pair accuracy on a held-out split of the human
curation pairs. A regression on the human axis is a blocker even when the Haiku
axis improves.

**Do not gate against `--mode baseline` on `mwmbl/resources/model.xgb`.** That
artifact was trained on *all* 849 LLM-labelled queries (see `git log` on it), so it
has seen every query in the held-out split and scores about 0.02 NDCG@10 too high.
Gate against `--exclude-pool`, which trains both arms on the same split and differs
only in the source being measured. Once a configuration wins, retrain it with
`--test-size 0` for shipping, as the deployed model was.

### What the Staan retrain measured

Same config, same split, trained with and without the Staan rows, both scored on the
same Staan-containing test pool:

| training data | NDCG@10 | human pair-acc |
|---|---|---|
| with Staan (`model-combined.xgb`) | 0.7612 | 0.805 |
| without Staan (`--exclude-pool staan`) | 0.7583 | 0.803 |

Marginal, and expected: the 50-feature model has no source feature, so Staan's prior
reaches it only through `score`. **The endpoint's value is recall, not the re-ranker.**
On the same 2% sample of the gold test set:

| arm | NDCG | ±SEM | gold recall | latency |
|---|---|---|---|---|
| standard search (index + wiki) | 0.386 | 0.043 | 4.8% | 0.004s |
| combined search, no Staan | 0.392 | 0.043 | 4.8% | 0.005s |
| combined search (index + Staan + wiki) | 0.583 | 0.030 | 20.0% | 2.14s |

Read the NDCG gain with its caveat: the gold set is scraped **Google** SERPs, and
Staan is a commercial web-search API whose results resemble Google's, so this metric
partly rewards agreeing with Google. The independent check is the blind Haiku judge,
which never sees Google and rates Staan at relevance 2.25 / overall 6.30 against the
Mwmbl index's 0.70 / 2.53. Both point the same way.

The latency is the cold Staan call; results are cached in the external results index,
so repeat queries do not pay it.
