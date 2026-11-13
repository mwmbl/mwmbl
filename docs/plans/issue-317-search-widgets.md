# Issue #317: Widgets in v2 standard search

A widget is a live answer from another site's service, such as a tannerd.net calculator or
an Open-Meteo forecast. It is shown above the results and always links back to that site.
Standard search must not get noticeably slower, and almost all requests come from bots and
SearXNG clients that never show a widget.

Search therefore only works out *which* widget applies, cheaply. A browser that is going to
show the widget fetches its data separately. Arithmetic is the one exception: it is computed
in place.

**Widget documents are filed under their own keys.** A widget is an ordinary index document
with `DocumentSource.WIDGET`. It is not filed under ordinary terms. It has a set of
*trigger tokens*, such as `{new, york, weather}` or `{:n, km, miles}`, and is stored under
exactly one key: `widget:` followed by a sorted pair of those tokens. The pair is the
spec's trigger word plus the instance's most distinctive token, e.g. `widget:weather york`.
A widget with a single trigger token gets a single-token key, e.g. `widget:calculator`.

**Lookup at search time works like this:**

1. Run the query through the *widget tokenizer*. It lowercases, drops stopwords, splits
   `10km` into `:n km`, and replaces values with placeholders: `:n`, `:money`, `:date` and
   `:time`. `%` is kept as a token of its own.
2. If none of the tokens is in the in-memory set of trigger words, stop. The set comes
   from the specs, so this takes about a microsecond and nearly every query stops here.
3. Otherwise, read every single-token key and every unordered pair of tokens, from at most
   6 tokens, so at most 21 page reads.
4. Keep a document only if *all* its trigger tokens are in the query. Return at most one
   per spec, and when a spec matches more than once, keep the first stored (cities are
   stored most populous first).

This handles word order ("weather london"), stopwords ("weather in london"), extra words
("london weather today") and multi-word places ("weather in new york"). York, New York and
York, PA can share the key `widget:weather york`, and the "all trigger tokens" check picks
the right one.

`TinyIndex.retrieve` already discards documents another term put on the same page, so
widget keys never show up among ordinary results. Pages are still shared by hash, though,
so widget documents are kept ahead of other documents when a page fills up.

**The search response carries stubs only.** It gets a list of `{type, title, url,
source_name}`. The stub's `url` is the site's own page with the inputs filled in, such as
tannerd.net's `share_url` or `https://open-meteo.com/en/docs?latitude=51.51&longitude=-0.13`.
It is both the link back and everything the widget endpoint needs.
`GET /api/v2/widgets/?url=&q=` matches the URL to a spec, takes the values from `q`, calls
the site with an SSRF guard and a Redis cache, and returns fields, inputs and links.

**Providers are data, not code.** Each provider is a declarative JSON spec checked into
`mwmbl/widgets/providers/`. It gives:

- the site and the API URL templates, with the API host the only one allowed
- typed inputs and the patterns that pull them from the query
- output fields as JSON paths, plus a display type: `fields`, `form` or `forecast`
- the trigger tokens and the cache TTL

A site is excluded if its robots.txt blocks Mwmbl. The specs move to Postgres when user
submissions arrive, in a later issue.

Searches with no trigger word gain one set-membership check. A widget query gains at most 21
page reads, and an arithmetic query gains one cached lookup. A CI benchmark over the rankeval
queries checks that the median search time does not move.

## PR 1: Widget tokenizer, widget keys and the WIDGET source
**Estimated added lines:** ~350
**Files:** mwmbl/widgets/__init__.py, mwmbl/widgets/tokens.py, mwmbl/widgets/keys.py,
mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_batches.py, mwmbl/format.py,
test/test_widgets_tokens.py, test/test_index_batches.py

- `widget_tokens(text)` implements the normalisation above. It is pure, and it is
  table-tested on "10km in miles", "$50 to eur", "20% of 150", "days until 2026-12-25",
  "weather in new york" and similar queries.
- `storage_key(trigger_tokens, anchor)` gives the one key a document is stored under.
  `query_keys(tokens)` gives the single-token and pair keys, capped at 6 tokens.
  Property test: any document whose trigger tokens are a subset of the query's tokens
  has its storage key in `query_keys`.
- Add `DocumentSource.WIDGET = 4` and `DOCUMENT_PROVIDERS[WIDGET] = "widget"`.
- `index_widget_documents(documents_with_keys, index_path)` writes each document under its
  key. In `sort_documents`, WIDGET documents go ahead of other documents, after curated
  ones.
- This touches the indexer, so the commit needs a `CRAWLER_VERSION` bump or the
  no-change line.

## PR 2: Arithmetic, computed inline, with "calculator" results beneath
**Estimated added lines:** ~300
**Files:** mwmbl/widgets/arithmetic.py, mwmbl/tinysearchengine/search.py,
test/test_widgets_arithmetic.py

- A regex rejects every query that is not numbers, spaces, `.+-*/^%()` and at least one
  operator. It takes under a microsecond.
- Queries that pass go through `ast.parse(q, mode="eval")` and a node walk. The walk
  allows numeric constants, `+ - * / % **`, unary minus and brackets. `^` means power.
  The arithmetic uses `Fraction`, so 0.1+0.2 gives 0.3. Never `eval`.
- Limits: 100 characters, an exponent of at most 1000 in absolute value, and results
  over 10^100 shown in scientific notation or rejected. `9**9**9` and the like are
  rejected without being computed.
- The response gets a complete `calculation` widget (`expression`, `result`), not a stub.
- A query like "164*8" has no useful terms, so the results are replaced by those for
  "calculator". These come from `ranker.search("calculator")` cached in memory for an
  hour, so an arithmetic query reuses one ranking pass rather than running a new one.
- Tests cover precedence, division by zero (no widget), the rejected inputs, and a
  benchmark that non-arithmetic queries pay only the regex.

## PR 3: Provider specs and widget stubs in search
**Estimated added lines:** ~400
**Files:** mwmbl/widgets/spec.py, mwmbl/widgets/lookup.py, mwmbl/tinysearchengine/search.py,
test/test_widgets_lookup.py

- A pydantic `WidgetSpec` is loaded from `mwmbl/widgets/providers/*.json` at startup.
  The set of trigger words and the URL-template index are built from it.
- `find_widgets(q, tiny_index)` follows the lookup steps above and runs before
  `ranker.search`.
- Add `widgets: list[Widget]` to `SearchResponse` as a union of the calculation widget
  and the stub. Document that clients may ignore widgets, or fill stubs from
  `/api/v2/widgets/`.
- Test that search makes no outbound call and no database query, with widgets in the
  index. Test that a query without a trigger word reads no widget keys.

## PR 4: The widget endpoint
**Estimated added lines:** ~450
**Files:** mwmbl/widgets/api.py, mwmbl/widgets/fetch.py, mwmbl/api.py, mwmbl/settings_common.py,
test/test_widgets_api.py

- `GET /api/v2/widgets/?url=&q=` matches `url` to a spec's page template, or returns
  404. Inputs are filled from the URL first, then the query, then the spec's defaults,
  and the API URL is built from them.
- Fetch rules:
  - https and GET only, to the spec's API host only
  - no redirects
  - a 2 s timeout and a 64 KB cap on the response
- Answers are cached in Redis, keyed on the spec and the inputs, for the spec's TTL. A
  `SET NX` lock makes concurrent misses share one upstream call. The endpoint has a rate
  limit per IP.
- Every request also checks the site's robots.txt for Mwmbl's user agent, cached for a
  day. A site that blocks us returns 404.
- The response is the stub plus:
  - `display`
  - `fields`: `[{label, value, unit}]`, plain text, which the client is told to escape
  - `inputs`, the schema of the spec's inputs
  - `share_url`, linked with `rel="noopener nofollow"`
- Tests use `pytest-httpx` and the `redis_cache` fixture.

## PR 5: tannerd.net provider and the widget indexing command
**Estimated added lines:** ~400
**Files:** mwmbl/widgets/openapi_import.py, mwmbl/widgets/providers/tannerd.json,
mwmbl/management/commands/index_widgets.py, test/test_widgets_tannerd.py

- `openapi_import` turns an OpenAPI GET operation into a draft spec. Its parameters
  become inputs, its examples become defaults, and one sample call gives the output
  fields. tannerd.net publishes its OpenAPI at `/api/openapi.json`: 25 read-only,
  keyless endpoints that send `access-control-allow-origin: *` and include `share_url`
  in every answer.
- Check in reviewed specs for calculators people plausibly search for:
  - percentage change: `{:n, percentage, change}`
  - date difference: `{days, until, :date}`
  - salary to hourly
  - trip cost
  - unit price compare
  - electricity cost
- `manage.py index_widgets` checks each provider's robots.txt. It skips a blocked site
  and purges its keys, then writes one document per spec instance. It reuses
  `purge_blacklisted`'s page rewrite to delete documents.

## PR 6: Open-Meteo weather provider
**Estimated added lines:** ~350
**Files:** mwmbl/widgets/providers/open_meteo.json, mwmbl/widgets/places.py,
mwmbl/management/commands/index_widgets.py, test/test_widgets_weather.py

- The spec takes `latitude` and `longitude` in its URL template. It shows current
  conditions and 3 days of forecast (`display: forecast`), with WMO weather codes turned
  into text. The cache lasts 30 minutes.
- `index_widgets --places <cities.tsv>` reads GeoNames cities15000, about 26k places
  under CC BY 4.0, downloaded at run time and never committed.
- Each place gets one document, titled "<Name> weather, <Country>". Its trigger tokens
  are `{weather}` plus the name's tokens, and its anchor is the name's rarest token.
  Places are written most populous first.
- Also index the place under `forecast`.

## PR 7: Front-end rendering
**Estimated added lines:** ~300
**Files:** front-end/…

- Render the calculation widget straight away. Fill stubs from the widget endpoint into
  space reserved in advance, so the page doesn't jump.
- Each display type gets its own component. Credit the site prominently and link it to
  `share_url`. If the fetch fails, hide the widget.
- If the front end renders results on the server, fill the stubs in-process there and
  skip the second round trip.

## Later (separate issue): user-submitted widget sites
- A submission takes a site URL. A worker finds the site's OpenAPI file, `llms.txt` or
  agent-skills file and drafts a spec with `openapi_import` plus an LLM.
- The spec goes to a moderation queue and is indexed only once a person approves it.
- A widget sits above the results, so it is valuable to spammers. Reviewers must check
  the trigger tokens, so a site can't claim popular queries, and that the API host is
  the submitter's own site.
- Sites without an API could be embedded in a sandboxed iframe that loads only on click,
  since loading it shares the viewer's IP with the site.

## To evaluate
- Whether a widget should be shown only when it, or the site behind it, would rank well.
  Keeping widgets under their own keys makes this easy to switch per spec.
