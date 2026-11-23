# Crawler API

The server's side of distributed crawling. The crawler volunteers run is the Rust one in
[`rust/crawler`](../../rust/crawler/README.md), which documents how to run and configure it.

This module holds:

- `app.py` - the crawler endpoints under `/api/v1/crawler/`: `POST /results`, where crawlers
  submit what they found and get the main index's results for the same terms back,
  `GET /curated-domains`, and `POST /errors`. `POST /results` answers 426 to a crawler
  older than the `MIN_CRAWLER_VERSION` setting.
- `batch.py` - the request and response schemas of those endpoints. The Rust crawler's side
  of them is pinned by `rust/crawler/fixtures/` and `test/test_rust_crawler_contract.py`.
- `stats.py`, `agreement.py`, `agreement_rollups.py`, `worst_agreements.py` - crawl
  statistics and how often crawlers' results agree with the main index.
- `retrieve.py`, `ssrf.py`, `urls.py` - the server's own fetching of other sites, for seed
  crawls, moderation evidence, widgets and super search.
- `release_check.py` - CI's check that a change to the crawler raises its version, which is
  what publishes a release.
