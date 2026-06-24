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
