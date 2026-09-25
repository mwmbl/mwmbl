# Index write-order A/B

One-off scripts behind `mwmbl/rankeval/index-write-order.md`, kept for provenance. The report
is reproduced from committed data by `evaluate.py report`. Large intermediate files (the
corpus and the two indexes, ~1.2 GB) go in `devdata/index_write_order/`, which is gitignored.

Run everything from the repository root with
`DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" PYTHONPATH=scripts/index_write_order:.`
and `uv run python`:

1. `build_corpus.py`: the documents that compete for the en-gb eval queries' pages, from the
   local crawl batches and the live index's cached pages (~25 minutes on 8 cores).
2. `build_index.py`, once with `INDEX_PAGE_RANKER=heuristic` and once with `INDEX_PAGE_RANKER=ltr`
   (~23 and ~8 minutes).
3. `PYTHONHASHSEED=0 evaluate.py rank`: the index-only top ten per arm, into
   `devdata/combined_providers_eval/index_write_order/`.
4. `evaluate.py batches`: blind UK-relevance batches, judged by Claude Haiku 4.5 subagents into
   `devdata/index_write_order/judge/uk_*.out.jsonl`.
5. `evaluate.py consolidate`: the judgments into
   `devdata/combined_providers_eval/haiku/relevance_engb_index_write_order.jsonl`.
6. `evaluate.py report` and `evaluate.py pages`.

Separately:

- `benchmark_rebuild.py N`: the per-document cost of re-filing documents under all their terms,
  per ranker, for the rebuild estimate.
- `census.py [STRIDE]`: read-only; counts the pages, entries and unique URLs of an index, meant
  for production.
