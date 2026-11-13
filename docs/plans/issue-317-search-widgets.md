# Issue #317: Widgets in v2 standard search

`GET /api/v2/search/` returns only `results` and `images` today, and it never calls out
or reads Postgres (`mwmbl/imagesearch/display.py`). This adds a `widgets` list to
`SearchResponse`: a calculation/unit-conversion answer using tannerd.net's calculations,
and a weather forecast from Open-Meteo. Search must stay fast, so:

- **Detection is local and cheap.** Each widget has a `match(q)` that is a regex or a
  small parser, a few microseconds, so queries that match nothing pay nothing.
- **Calculations never call out.** We compute them in-process, port tannerd.net's
  formulas, and link to tannerd.net as the source. That sends traffic to the site, which
  answers the concern raised on #317.
- **Weather is never on the critical path.** Place names are resolved from a bundled
  gazetteer. The forecast fetch runs in parallel with `ranker.search` and waits at most
  as long as ranking takes. If the cache misses or the fetch is late, the widget goes out
  as `pending` with a `fetch_url` the client loads on its own.

Response shape (a discriminated union on `type`):

```json
"widgets": [
  {"type": "calculation", "expression": "164*8", "result": "1312",
   "source_name": "tannerd.net", "source_url": "https://tannerd.net/..."},
  {"type": "weather", "status": "ready" | "pending", "location": {...},
   "current": {...}, "daily": [...], "fetch_url": "/api/v2/widgets/weather?lat=..&lon=..",
   "source_name": "Open-Meteo", "source_url": "https://open-meteo.com/"}
]
```

Latency budget: under 1 ms p99 added for queries that match nothing (CI asserts this with
a microbenchmark of the matchers over the rankeval query set). Under 0.5 ms for a
calculation. For weather, zero added in the worst case, because the request waits on
`max(ranking, cache lookup)` and never on the network.

## PR 1: Widget framework and the calculation widget
**Estimated added lines:** ~350
**Files:** mwmbl/widgets/__init__.py, mwmbl/widgets/base.py, mwmbl/widgets/calculation.py,
mwmbl/tinysearchengine/search.py, test/test_widgets_calculation.py

- `Widget` protocol: `match(q) -> Match | None`, then `build(match) -> WidgetSchema`. A
  registry runs the matchers in order and keeps at most one widget of each type.
- The calculation parser is a safe evaluator over `ast` that allows only numeric literals,
  `+ - * / ** %` and parentheses, plus a whitelist of functions (`sqrt`, `sin`, …) and
  constants. It caps the exponent and the input length, so `9**9**9` cannot hang a worker.
  Never `eval`.
- Add `widgets: list[Widget] = []` to `SearchResponse` and document it in the endpoint
  description: escape the text, and link `source_url` with `rel="noopener nofollow"`.
- Tests cover the expressions, the inputs it rejects (names, attributes, huge powers, very
  long input) and a matcher microbenchmark.

## PR 2: Unit and percentage conversions (tannerd.net calculations)
**Estimated added lines:** ~400
**Files:** mwmbl/widgets/conversion.py, mwmbl/resources/units.json, test/test_widgets_conversion.py

- Handle "10 km in miles", "20% of 150", "100 f to c". The unit table is static data;
  where the formulas follow tannerd.net, take them from there with attribution.
- Currency is left out on purpose, because it needs live rates (a separate decision).

## PR 3: Local place-name resolver
**Estimated added lines:** ~300
**Files:** mwmbl/widgets/places.py, mwmbl/resources/cities.tsv (GeoNames cities15000,
trimmed to name/alt names/country/lat/lon/population, ~2 MB), test/test_widgets_places.py

- Load the table once at startup into a dict from normalised name to the most populous
  place.
- Match `weather <place>`, `<place> weather` and `forecast <place>`. Never use the
  requester's IP, so no location leaves the query text.

## PR 4: Open-Meteo client with Redis cache and the async weather endpoint
**Estimated added lines:** ~400
**Files:** mwmbl/widgets/weather.py, mwmbl/tinysearchengine/search.py (new route
`/api/v2/widgets/weather`), mwmbl/settings_common.py, test/test_widgets_weather.py

- Fetch with `httpx` and a 1.5 s timeout. Cache in Redis under the key
  `weather:{lat:.2f}:{lon:.2f}` with a 30-minute TTL, which Open-Meteo's hourly model
  makes ample.
- Coalesce requests by setting a short Redis lock with `SET NX`, so one cache miss for
  "weather london" makes one upstream call, not N.
- The endpoint returns the ready widget. Rate limit it like search, and accept only
  coordinates that are in the gazetteer, so it cannot be used as an open proxy.
- Tests use `pytest-httpx` and the `redis_cache` fixture.

## PR 5: Weather in search without added latency
**Estimated added lines:** ~200
**Files:** mwmbl/tinysearchengine/search.py, mwmbl/widgets/base.py, test/test_widgets_weather.py

- When the weather matcher fires, read the Redis cache in the same thread first (about
  0.2 ms). On a hit, the widget is `ready`.
- On a miss, submit the fetch to a small module-level `ThreadPoolExecutor` and run
  `ranker.search` at the same time. Once ranking is done, wait on the future with
  `timeout=0`. If the fetch has not finished, return `pending` plus `fetch_url`. Let the
  fetch finish and fill the cache anyway.
- Add a test asserting the response time does not grow when upstream takes 2 s (mock it).

## PR 6: Front-end rendering
**Estimated added lines:** ~250
**Files:** front-end/…

- Render the widgets above the results, and fetch `fetch_url` for `pending` weather. Show
  the source attribution on every widget.

## Open questions
- **tannerd.net:** the session's network policy blocked it, so I could not see what it
  offers. The plan assumes we port its formulas and link to it. If it has an API, or a
  licence that rules porting out, PR 1 and PR 2 change.
- **Open-Meteo terms:** the free API is non-commercial only. Paid API v2 customers would
  probably count as commercial use, which means a subscription or self-hosting.
- **#317's objection:** do widgets go against "send users to sites"? Attribution links
  soften this. A flag in settings would let us turn the widgets off per type.
