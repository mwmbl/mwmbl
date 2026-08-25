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

- **Wikipedia results from the index** —
  `mwmbl/rankeval/evaluation/evaluate_wiki_index.py` measures what it costs in
  NDCG to stop calling the Wikipedia search API on every query: results a call
  returns are written into the index under the query's unigrams and bigrams, and
  a later query that the index already answers with enough Wikipedia results
  skips the call. It sweeps the gate definition and threshold as arms over one
  shared query sample, and reports NDCG, calls avoided, and NDCG **on the subset
  where the gate fired** — the number the feature is decided on.

```bash
DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.evaluate_wiki_index --fraction 0.1
```

  Reads go through an `OverlayIndex`: the remote production index unioned with a
  fresh local `TinyIndex` that everything written during the run goes into (the
  local dev index is far too small for a gate to mean anything). Wikipedia is
  called at most once per distinct query ever — responses are cached in
  `devdata/wiki-index-eval-cache`, while the *count* of calls each policy would
  make is tracked exactly. Results in `WIKI_INDEX_CACHE_FINDINGS.md`.

  `--arm-set scores` asks the other question: with Wikipedia called on *every*
  query so nothing is skipped, does storing its results help or hurt the queries
  that later retrieve them? The arms vary only which query term a stored result's
  score is filed under (`none` / `all` / `specific` / `exact`) and whether
  re-storing overwrites or averages, and the report is a paired per-query Δ on the
  queries that actually retrieved another query's stored documents.

```bash
DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.evaluate_wiki_index \
      --arm-set scores --fraction 0.3
```
