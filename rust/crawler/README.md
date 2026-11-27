# Mwmbl crawler

The crawler volunteers run to build Mwmbl's index. It crawls the web, keeps a local index of
what it finds, and submits the results worth having to the Mwmbl API, which merges them into
the main index. Run it with the `docker-compose.yml` at the root of the repository; the image
is `mwmbl/crawler:stable` and is built by `Dockerfile.crawler`.

## Configuration

Every option is an environment variable as well as a flag; `mwmbl-crawl --help` lists them.

Required:
- `MWMBL_API_KEY` - the API key results are submitted with.
- `MWMBL_CONTACT_INFO` - an email address or URL for site administrators, sent in the
  User-Agent of every fetch so they can reach you if the crawler causes them trouble.

Optional:
- `MWMBL_REMOTE_SERVER` (default `https://api.mwmbl.org`) - the API to submit to.
- `MWMBL_DEVICE_NAME` - the name this crawler shows as in your device list. Left unset or
  empty, a random one is generated and kept in the data directory. The machine's hostname is
  never used, since hostnames routinely carry their owner's name.
- `CRAWL_MAX_TASKS` (default 64) - pages fetched at once.
- `CRAWL_THREADS` (default 32) - threads running the crawl.
- `BLOCKING_THREADS` (default 8) - threads parsing pages.
- `INDEX_THREADS` (default 2) - threads reading and writing the local index, each owning a
  share of its pages.
- `SUBMIT_MAX_TASKS` (default 16) - batches of results submitted at once.
- `CRAWL_DELAY_SECONDS` (default 0) - seconds between starting one fetch and the next, with
  10% random fuzz.

Data lives in `~/.mwmbl`: the local index (`crawl-index.tinysearch`, about 400MB), the bloom
filter of URLs seen and domain links (`crawl-bloom`) and the device name.

## Politeness

- robots.txt is fetched before any page of a domain and cached for a day. If it cannot be
  fetched (a 5xx, a 429, a timeout) nothing on the domain is fetched and it is retried after
  an hour. A 4xx other than 429 means there is no robots.txt and everything is allowed.
- Only one fetch per domain is in flight at a time, and after it the domain waits out the
  `Crawl-delay` its robots.txt asks for, or 5 seconds. A domain that answers outside 2xx is
  left alone for a minute, doubling with each refusal up to six hours.
- Pages that answer outside 2xx are not indexed and their links are not followed, so error
  pages and bot challenges stay out of the index.

## Your network

The crawler only fetches public internet addresses, so a crawled link cannot reach your
router, other machines on your network or a cloud metadata endpoint. A URL or redirect whose
host is an IP address is refused, and so is a hostname that resolves to a private, loopback,
link-local, shared (100.64.0.0/10), multicast or reserved address, IPv4 or IPv6. The check is
made when the name is resolved, so it covers robots.txt and every redirect, and the address
checked is the one connected to. The API in `MWMBL_REMOTE_SERVER` is exempt. The optional
`servo` build renders pages with its own networking, which this check does not cover.

## What it submits

Every 100 pages crawled, the crawler indexes them locally, and for the 100 terms with the most
new documents it submits what its index holds for each term that is not already from the main
index (`POST /api/v1/crawler/results`). The server answers with the main index's results for
those terms, which the crawler merges into its own index, marked as synced, so they are not
submitted back.

The request and response formats are pinned in `fixtures/`, which both the crawler's tests and
the server's (`test/test_rust_crawler_contract.py`) check.

## Development

```sh
make rust-core   # fmt, clippy and the tests of this crate and mwmbl_core
```

The tests crawl a local HTTP server only. `.github/workflows/crawler-smoke.yml` builds the
image and runs it against the live web and a stand-in API until it has submitted and synced a
batch.

## Releases

The version in `Cargo.toml` is the release version. Raising it on main publishes
`mwmbl/crawler:X.Y.Z`, `:X.Y`, `:stable` and `:latest` (`.github/workflows/crawler-release.yml`),
and CI fails a pull request that changes the crawler without raising it
(`mwmbl/crawler/release_check.py`). A commit that leaves the crawler's behaviour unchanged
can say `No functional changes to the crawler.` on a line of its own instead.
