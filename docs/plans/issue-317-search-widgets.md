# Issue #317: Widgets in v2 standard search

A widget is a live answer from another site's service, such as a tannerd.net calculator or
an Open-Meteo forecast. It is shown above the results and always links back to that site.
Standard search must not get slower, and almost every request comes from a bot or a SearXNG
client that would never show a widget. So the work is split in two:

1. **Search does no new work.** Widgets are ordinary documents in the index with a new
   `DocumentSource.WIDGET`, filed under the terms they answer ("london weather", "weather
   london", "percentage change"). Ranking retrieves them like any other page. Standard
   search pulls them into a `widgets` list the same way `build_image_strip` pulls out
   Commons files, which costs one comparison per result. Nothing calls out, and nothing
   reads Postgres.
2. **The front end asks for the live data.** `GET /api/v2/widgets/?url=<widget url>&q=<query>`
   calls the provider, caches the answer and returns it in a standard shape. Only a browser
   that is going to show the widget makes this request.

**Widgets are data, not code.** Each provider is described by a declarative spec stored in
Postgres:

- the site's name and home page
- a page URL template, which is the link back
- an API URL template, which must be on the same host
- typed parameters, each with an optional pattern for pulling it out of the query
- output fields as JSON paths with labels and units
- how long to cache the answer, and which terms to index it under

Adding a provider means adding a spec, with no new Python. That is what lets a site be
submitted later and have an AI draft its spec. tannerd.net already publishes everything a
draft needs: an OpenAPI file at `/api/openapi.json`, `llms.txt` and agent-skills metadata.

**A widget document's URL is the site's own page with the parameters filled in.** For
tannerd.net that is its `share_url`, for example
`https://tannerd.net/tools/percentage-change-calculator/`. For a weather location it is
`https://open-meteo.com/en/docs?latitude=51.51&longitude=-0.13`. The widget endpoint
therefore needs no lookup per instance: it matches the URL to a spec by its template and
reads the parameters from the URL itself. The result link is also exactly the link back.

What we know about the providers:

- **tannerd.net.** About 25 free calculator endpoints that need no key and send
  `access-control-allow-origin: *` and `max-age=300`. Every answer carries `source`,
  `share_url` and `citation`. `robots.txt` allows `use=full`. Most calculators need
  several inputs, so the widget is mainly a small form generated from the OpenAPI
  parameters, pre-filled from the query where possible ("percentage change 50 to 75",
  "days until 2026-12-25", "salary to hourly 50000").
- **Open-Meteo.** One forecast API covering every location. Instances come from the
  GeoNames cities15000 list (about 26k places, CC BY 4.0, so it needs attribution). Its
  free tier is non-commercial; see the open questions.

The server makes these calls rather than the browser, even where CORS would allow the
browser to. That way the viewer's IP is never sent to a third party, one cache serves
everyone, and the endpoint is the only place a spec's output is turned into text.

## PR 1: WIDGET document source and keeping widget documents on index pages
**Estimated added lines:** ~200
**Files:** mwmbl/tinysearchengine/indexer.py, mwmbl/indexer/index_batches.py, mwmbl/format.py,
test/test_index_batches.py

- Add `DocumentSource.WIDGET = 4` and `DOCUMENT_PROVIDERS[WIDGET] = "widget"`.
- `index_documents_against_terms(documents_by_term, index_path)` files a document under
  exactly the terms it is given. Widgets need this because their terms are not derived
  from their text.
- In `sort_documents`, place WIDGET documents ahead of other documents, after curated
  ones, so a crowded 4 KB page never drops them. Their extracts are short, so the space
  they take is small.
- This touches shared indexer code, so raise `CRAWLER_VERSION` or include the no-change
  line in the commit message, whichever applies.

## PR 2: Widget specs and the widget strip in standard search
**Estimated added lines:** ~400
**Files:** mwmbl/widgets/models.py, mwmbl/widgets/spec.py, mwmbl/widgets/display.py,
mwmbl/migrations/…, mwmbl/tinysearchengine/search.py, test/test_widgets_display.py

- Two models: `WidgetProvider` (name, site URL, allowed API host, approved flag) and
  `WidgetSpec`. The spec is JSON validated by a pydantic schema, plus `index_terms`.
- `build_widget_strip(results, query)` mirrors `build_image_strip`. It takes WIDGET
  documents out of the results and keeps at most one per spec.
- For a strict trigger such as weather, all of the widget document's own tokens must
  appear in the query. This stops "york weather" from returning New York.
- Only `{type, title, url, source_name}` comes from the index, so this step never reads
  Postgres. The source name is derived from the URL's host.
- Add `widgets: list[WidgetStub]` to `SearchResponse`. Document that clients call the
  widget endpoint to fill a stub, and that it is safe to ignore.
- Test that search makes no outbound call and no database query when widgets are
  present, using `django_assert_num_queries(0)` and `pytest-httpx` with nothing
  registered.

## PR 3: The widget endpoint
**Estimated added lines:** ~450
**Files:** mwmbl/widgets/api.py, mwmbl/widgets/fetch.py, mwmbl/api.py, mwmbl/settings_common.py,
test/test_widgets_api.py

- `GET /api/v2/widgets/?url=&q=` matches the URL to an approved spec, or returns 404.
  Specs are cached in memory and refreshed every minute.
- It fills the parameters from the URL first, then from the query patterns, then from
  the spec's defaults. It then builds the API URL from the template.
- SSRF rules: the API host must equal `WidgetProvider.allowed_host`, only https and GET
  are allowed, there are no redirects, the timeout is 2 s and the response is capped at
  64 KB.
- Answers are cached in Redis, keyed on the spec and the parameters, using the spec's
  TTL. A `SET NX` lock makes concurrent misses share one upstream call. The endpoint is
  rate-limited per IP.
- The response is the stub plus:
  - `fields` as `[{label, value, unit}]`, plain text, with the client told to escape it
  - `inputs`, the parameter schema, so the client can draw a form
  - `source_url` and `share_url`, the link back, using `rel="noopener nofollow"`
- Tests use `pytest-httpx` and the `redis_cache` fixture.

## PR 4: tannerd.net provider
**Estimated added lines:** ~350
**Files:** mwmbl/widgets/providers/tannerd.json, mwmbl/management/commands/index_widgets.py,
mwmbl/widgets/openapi_import.py, test/test_widgets_tannerd.py

- `openapi_import` turns an OpenAPI GET operation into a draft spec. Its parameters
  become inputs, its examples become defaults and its summary becomes the title. The
  output fields come from one sample call. A person reviews the draft.
- Check in the reviewed specs for 5–10 calculators that people plausibly search for:
  percentage change, date difference, salary to hourly, trip cost, unit price compare
  and electricity cost. Each gets query patterns and index terms.
- `manage.py index_widgets` writes one document per spec to the index, using PR 1.

## PR 5: Open-Meteo weather provider
**Estimated added lines:** ~350
**Files:** mwmbl/widgets/providers/open_meteo.json, mwmbl/widgets/places.py,
mwmbl/management/commands/index_widgets.py, test/test_widgets_weather.py

- The spec has a URL template with `latitude` and `longitude` and fields for current
  conditions plus a 3-day forecast. The cache TTL is 30 minutes.
- `index_widgets --places <cities.tsv>` downloads GeoNames at run time and keeps it out
  of the repo. For each place it writes one document, titled "<Name> weather, <Country>",
  under "<name> weather" and "weather <name>". Where two places share a name, it keeps the
  one with the larger population.
- Before indexing, check how the tokenizer handles "weather in london" and "london
  weather today" against the query log. Add term variants only where the log shows the
  searches exist.

## PR 6: Front-end rendering
**Estimated added lines:** ~300
**Files:** front-end/…

- For each stub, call the widget endpoint and render the fields, plus the form when the
  widget has inputs. Credit the site prominently and link it to `share_url`.
- If the call fails, hide the widget without showing an error.

## Later (separate issue): user-submitted widget sites
A submission takes a site URL. A Seed Search-style worker finds the site's OpenAPI file,
`llms.txt` or agent-skills file and drafts a spec with `openapi_import` plus an LLM. The
spec goes to a moderation queue (`mwmbl/moderation/`) and is indexed only after a person
approves it. These rules are already in the PR 2 and 3 schema:

- the API host must be the submitter's own site
- the endpoints are read-only
- outputs are text only, never HTML
- index terms are reviewed, so a submission can't take over popular queries

## Open questions
- **Open-Meteo licence.** Its free API is non-commercial. Widgets shown on mwmbl.org are
  probably fine, but API v2 customers are paying. Stubs carry no Open-Meteo data, so they
  may be outside its terms. Still, either confirm with Open-Meteo or limit
  `/api/v2/widgets/` to the mwmbl.org front end.
- **The objection on #317.** A widget answers the question, and the link back may not
  make up for the visit the site loses. tannerd.net says it wants agent and AI use
  (`use=full`). Should providers have to opt in, for example by being listed in their
  `robots.txt` or `llms.txt`, before we add them?
- **Ranking.** The plan returns any widget document that retrieval finds, with no ranker
  involvement. Alternatively, a widget could be shown only if it ranks in the top 10, as
  the image strip does.
