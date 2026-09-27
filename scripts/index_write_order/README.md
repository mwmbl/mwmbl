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

The crawl model (`mwmbl/rankeval/index-write-order-crawl-model.md`) adds two write-time arms,
`INDEX_PAGE_RANKER=crawl` and `crawl-domain`, which `build_index.py` swaps in from
`crawl_ranker.py`:

1. `run_crawl.py --export-queue`, then `run_crawl.py` with `CRAWL_SUBMIT_MODE=off`: crawled
   batches, saved before anything ranks them, into `devdata/index_write_order/crawl/`.
2. `crawl_model.py serp`: each host's Google SERP appearances in `scripts/downloads`.
3. `crawl_model.py negatives`, then `crawl_model.py train [--no-domain]`: the offline gate and
   the models.
4. `build_index.py` for each crawl arm, then steps 3-6 above. `batches` writes only the URLs
   not yet graded. `evaluate.py attribution` shows whether missing good results were evicted
   or outranked.

`INDEX_WRITE_ORDER_RUN=fresh` points every script at `devdata/index_write_order/fresh/`, for a
rerun on crawl captured from 2026-09-25 on.

Cap memory for the long runs, for example with `systemd-run --user -p MemoryMax=6G`. The crawler
and feature extraction together can exhaust a 16 GB machine.

Separately:

- `benchmark_rebuild.py N`: the per-document cost of re-filing documents under all their terms,
  per ranker, for the rebuild estimate.
- `census.py [STRIDE]`: read-only; counts the pages, entries and unique URLs of an index, meant
  for production.
