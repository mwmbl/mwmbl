# Initial Brainstorm

We can self-host image search. The hard parts are safe search and serving thumbnails, not search itself. And Brave can't be the bootstrap: its terms forbid it. Other free, open sources can.

## How our own image search would work

**1. Find images while we crawl.** The volunteer crawler already downloads every page, but a crawl batch only keeps the title, a text extract and the links (`batch.py`). The images are thrown away. We could add an `images` list to `ItemContent`, holding for each image:
- its URL;
- its alt text, caption or nearby text;
- its width and height, if the page gives them;
- whether it's the page's `og:image`.

This costs almost nothing, because the page has already been downloaded. The catch is that volunteers' crawler clients would need updating, and their batches would get bigger.

**2. Search images by their text.** Mature image search ranks mostly by the words around an image, not by what's in the picture. That fits what Mwmbl already does: images would be documents whose text is their alt text, caption and page title.
- They'd go in a separate index, not the main one. The repo already learned that mixing other entries into the search index pushes real pages out (#357 and #359, cited in `external_cache.py`).
- Ranking could start simply: text match, how trusted the page is, image size (skip icons and tracking pixels), and removing duplicate copies of the same picture.
- Searching by what's actually in the picture (embedding models such as CLIP) is a later step. It needs GPUs and a new kind of index.

**3. Safe search has to be ready before anything is shown.** This is the real gate. Brave does it for us now. We'd need to do it ourselves:
- Domain blocklists, which we already have.
- A nudity/adult classifier run on every image. That means downloading every image, not just reading its text.
- **Matching against known child-abuse image databases.** This is a legal obligation once we host or serve images, and it means applying to programmes such as PhotoDNA or IWF. This is non-negotiable.

Strict filtering would stay the only mode, as it is now.

**4. Serve thumbnails through our own proxy.** Linking directly to the original image would leak users' IP addresses to every site, and burden those sites. Brave serves thumbnails through its own proxy for exactly this reason, so we'd need one too. That is the main running cost: bandwidth, plus a cache in front. Copyright for thumbnails has been treated as fair use in the US (Perfect 10 v. Google) but is less settled in the EU and UK, so take legal advice.

## Can Brave bootstrap it?

Not by reusing its results. The Search API Terms of Use, section 3(b), forbid us to:
- (i) store, cache or build a database of results, beyond transient storage;
- (ii) create derivative works of the results;
- (x) "use the API ... to replicate or attempt to replace the functionality of the API";
- (xiii) use results to "create, evaluate, train ... or otherwise improve" AI models.

Building our index from Brave's results breaks (i), (ii) and (x) directly. Even crawling the pages Brave returns, as Seed Search does with Staan, would mean storing parts of Brave's results to feed our index. Using Brave to score our ranking model probably falls under (xiii). I'd treat all of these as off-limits unless Brave agrees in writing.

**What Brave can legitimately do:**
- **Cover the gaps while we grow.** Search our own image index first and call Brave only when ours has too few results. That's the same shape as Combined Search with Staan.
- **Show progress.** The costs dashboard would then show Brave calls falling as our coverage grows.
- **Measure demand.** Daily Brave calls tell us whether image search is worth building at all.

## Better bootstrap sources (open and free)

- **Our own crawl:** re-extract images from the pages already in our index.
- **Wikimedia Commons:** about 100M images with good captions and clear licences.
- **Openverse:** an API for Creative Commons-licensed images, run by WordPress.
- **Common Crawl:** its metadata (WAT) files include image links and alt text for billions of pages.
- **Avoid LAION.** It was withdrawn after child-abuse material was found in it.

## A rough path

1. Keep the Brave version running and watch demand on the dashboard.
2. Add image extraction to the crawler, and build a text-only image index from our crawl plus Wikimedia Commons and Openverse.
3. Build the safe-search pipeline (blocklists, classifier, child-abuse hash matching) and the thumbnail proxy. Nothing is served until these exist.
4. Serve our index first with Brave as fallback, and judge quality with the existing LLM-judge approach, scoring our results on their own without comparing them to Brave.
5. Later: search by image content, and better duplicate removal.

Steps 2 and 4 are mostly the kind of work Mwmbl already does. Step 3 is new territory, with real legal and operating costs, and it's what decides whether this is worth doing. 

-----


# Detailed Plan

Image search today is the Brave-backed proof of concept: `GET /api/v1/images/search` in
`mwmbl/image_search.py`, cached for an hour, with call costs on `/admin/provider-costs/`.
That lands separately, by hand, and is a prerequisite only for PR 10. When this plan is
finished, Mwmbl answers image searches from an image index of its own, built from its own
crawl plus Wikimedia Commons and Openverse. Brave is called only when our index has too
few results. No image from our index is shown until it has passed blocklists, an
adult-content classifier and a CSAM hash match, and thumbnails come from our own proxy, so
users' IP addresses never reach image hosts. The front-end lives in a separate repository
and is out of scope here.

**Constraints every section must respect:**

- **Never build from Brave's results.** The Brave Search API Terms of Use, section 3(b),
  forbid storing, caching or building a database of Search Results beyond transient
  storage (i), derivative works (ii), using the API to replace it (x), and using results
  to evaluate or improve models (xiii). Nothing Brave returns is written to the image
  index, used as a crawl seed, or used to judge our ranking.
- **Our images stay separate from the main index.** The image index is a file of its own,
  like `mwmbl/indexer/external_cache.py`, because entries sharing the main index push real
  pages out (#357, #359).
- **Fail closed.** An image whose safety checks have not all passed is never served, and
  with no CSAM hash-matching provider configured, nothing from our index is served at all.
- **Do not use LAION.** It was withdrawn after CSAM was found in it.

**Human prerequisite:** before PR 8 can be switched on, someone must get access to a CSAM
hash-matching service (for example PhotoDNA, or the IWF hash list). Until then PR 8 ships
with its gate closed.

## PR 1: Extract image candidates in the crawler
**Estimated added lines:** ~300
**Files:** mwmbl/crawler/batch.py, mwmbl/crawler/retrieve.py, test/test_retrieve_images.py

Add an optional `images` list to `ItemContent`, with one `ImageCandidate` per image:

- `url`;
- `alt_text`;
- `caption` (from `<figcaption>`);
- `width` and `height`, when the markup declares them;
- `is_og_image`.

Fill it in `retrieve.py` from the HTML the crawler already has. Resolve relative and
`srcset` URLs to absolute ones. Drop `data:` URIs, and images declared smaller than 100px
on either side. Cap the list at 20 per page. The field is optional, so batches from older
volunteer clients still validate.

- A page with `<img>`, `<figure>`/`<figcaption>` and `og:image` yields the expected
  candidates.
- Icons, tracking pixels and `data:` URIs are dropped, and the list is capped.
- A batch without `images` still validates and indexes as before.

## PR 2: Store image candidates in an image index of their own
**Estimated added lines:** ~400
**Files:** mwmbl/indexer/image_index.py, mwmbl/indexer/index_batches.py, mwmbl/settings_common.py, mwmbl/settings_test.py, test/test_image_index.py

Add an `ImageDocument` dataclass, with:

- `image_url` and `page_url`;
- `text`: the alt text, caption and page title joined;
- `width` and `height`;
- `source`: a new enum covering crawl, Wikimedia and Openverse;
- `safety_status`: pending, approved or rejected; every new entry starts as pending;
- `last_seen`.

Store it in a separate `TinyIndex` file, configured by new `IMAGE_INDEX_NAME` and
`IMAGE_INDEX_NUM_PAGES` settings. Key it by the terms of `text`, using the tokenizer the
main index uses.

When crawl batches are indexed, write their image candidates here too, deduplicated by
`image_url`. Writing an image again must never reset its `safety_status`.

- Indexing a batch with images adds them to the image index and leaves the main index
  unchanged.
- Re-indexing the same image updates `last_seen` and keeps its safety status.
- Retrieval by a term from the alt text finds the image.

## PR 3: Import images from Wikimedia Commons
**Estimated added lines:** ~300
**Files:** mwmbl/management/commands/import_wikimedia_images.py, mwmbl/indexer/image_sources/wikimedia.py, test/test_import_wikimedia_images.py

Add a management command that pages through the Commons API, or a Commons dump, for a
given category or set of categories. It stores each file as an `ImageDocument` with
`source` set to Wikimedia, its caption and description as `text`, and its licence and
author kept for attribution. Rate-limit it, make it resumable, and send a descriptive
`User-Agent`, as `rank.py` does for Wikipedia.

- A canned API page is imported as the expected documents, with attribution.
- Re-running is idempotent, and the import resumes where it stopped.

## PR 4: Import images from Openverse
**Estimated added lines:** ~250
**Files:** mwmbl/management/commands/import_openverse_images.py, mwmbl/indexer/image_sources/openverse.py, test/test_import_openverse_images.py

The same as PR 3, for the Openverse API, with its key set in a new `OPENVERSE_API_KEY`
setting. Keep only licences that allow display with attribution, and keep that
attribution.

- A canned response is imported with its licence and attribution.
- Images under licences that are not allowed are skipped.

## PR 5: Fetch, measure and thumbnail images in the background
**Estimated added lines:** ~450
**Files:** mwmbl/indexer/image_fetch.py, mwmbl/background.py, mwmbl/settings_common.py, test/test_image_fetch.py

Add a background task that takes pending images in batches, fetches them through the
crawler's SSRF guard (`mwmbl/crawler/ssrf.py`), and then:

- rejects anything that is not a JPEG, PNG, WebP or GIF, or is too small;
- records the real dimensions and a perceptual hash;
- writes a 500px-wide WebP thumbnail under `DATA_PATH`, with the total size capped by an
  `IMAGE_THUMBNAIL_MAX_BYTES` setting.

Rate-limit the fetches per domain. An image this task has not processed stays pending.

- A canned image yields its dimensions, perceptual hash and a thumbnail.
- An unreachable, oversized or non-image URL is rejected, never retried forever, and never
  served.
- Thumbnails stop being written once the size cap is reached.

## PR 6: Domain blocklists for images
**Estimated added lines:** ~200
**Files:** mwmbl/indexer/image_safety.py, mwmbl/indexer/image_fetch.py, test/test_image_safety.py

Reject any image whose image URL or page URL is on a blocked domain, using the same
blocklists as web search (`mwmbl/indexer/blacklist_providers.py` and the blacklist
snapshot). Do this before fetching, so blocked hosts are never contacted.

- An image hosted on a blocklisted domain, or found on a blocklisted page, is rejected
  without being fetched.

## PR 7: Classify fetched images for adult content
**Estimated added lines:** ~400
**Files:** mwmbl/indexer/image_safety.py, mwmbl/indexer/image_fetch.py, pyproject.toml, test/test_image_safety.py

Run an open-weights adult-content classifier on every fetched image, and reject anything
over a threshold. Agree the model and the threshold on #550 before building. If the run
cannot choose one, it asks there. The model is loaded only by the background worker, as
the moderation model is.

- Images over the threshold are rejected, and those under it move on to the next check.
- With the classifier unavailable, images stay pending rather than being approved.

## PR 8: CSAM hash matching, and the approval gate
**Estimated added lines:** ~300
**Files:** mwmbl/indexer/image_safety.py, mwmbl/settings_common.py, test/test_image_safety.py

Add a client for a hash-matching provider, configured by a new
`IMAGE_CSAM_HASH_PROVIDER` setting and its credentials. An image becomes approved only
after it has passed the blocklists, the classifier and the hash match. A match is
rejected, deleted along with its thumbnail, and logged for reporting, without the image
URL ever reaching a normal log line.

With no provider configured, nothing is ever approved. This is the human prerequisite at
the top of this plan.

- With no provider configured, no image is approved, whatever else it passed.
- A match is rejected and its thumbnail deleted.
- An image that passes every check is approved.

## PR 9: Serve thumbnails through our own proxy
**Estimated added lines:** ~250
**Files:** mwmbl/image_search.py, mwmbl/urls.py, test/test_image_thumbnails.py

Add `GET /api/v1/images/thumbnail/<id>`, which serves the stored thumbnail of an approved
image with long-lived `Cache-Control` headers. Anything that is not approved returns 404,
and the proxy never fetches the original on request.

- An approved image's thumbnail is served with caching headers.
- Pending, rejected or unknown images return 404.

## PR 10: Answer from our own index first, with Brave as the fallback
**Estimated added lines:** ~350
**Files:** mwmbl/image_search.py, mwmbl/provider_usage.py, mwmbl/admin_views.py, mwmbl/templates/admin/provider_costs.html, mwmbl/settings_common.py, test/test_image_search.py, test/test_provider_usage.py

Needs the Brave proof of concept (`mwmbl/image_search.py`, `mwmbl/provider_usage.py`)
merged first. Behind a new `IMAGE_SEARCH_OWN_INDEX` setting, off by default:

- Search the image index for approved images, ranked by text match, size and page trust,
  with near-duplicates collapsed by perceptual hash.
- Return those results, with thumbnail URLs pointing at PR 9's proxy.
- Call Brave only when fewer than a configurable number are found. Never merge Brave's
  results into our index, or store them beyond the existing short-lived cache.

Count searches answered from our own index, and show them on the provider costs page next
to the Brave calls they replaced.

- With the setting off, behaviour is unchanged.
- With it on, enough approved results mean no Brave call. Too few mean exactly one Brave
  call, the results are returned, and nothing from Brave is written to the image index.
- Pending and rejected images never appear in results.

## PR 11: Judge our image results
**Estimated added lines:** ~300
**Files:** mwmbl/rankeval/evaluation/image_judge.py, test/test_image_judge.py

Add an evaluation that sends our own top image results for a fixed query set to an LLM
judge, which scores each list on its own for relevance, variety and the quality of its
text. It must not compare against Brave's results or use them as a reference; see
constraint (xiii) above. Report the scores per query and in aggregate.

- A run over canned results produces per-query and aggregate scores.
- Nothing in the evaluation calls Brave or reads Brave results.
