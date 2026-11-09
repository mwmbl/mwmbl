# Images in Seed Search: evaluation

## Query gate and image ranking

This section asks two questions for #610. Can Jev tell which queries want images? And once a query does, which
Commons images should it show? Everything is measured on the 849 train queries against Sonnet reference labels.
The code is `mwmbl/rankeval/seed_images/` (`run_relevance`, `label_batches`, `labels`, `gate_analysis`), and the
data is under `devdata/seed_images/gate/`.

**What "need" measures here.** The evaluation is specific to Commons, including the need labels. The Sonnet
rubric asks whether a strip of Commons pictures would improve the page, and says to give 0 when the subject is
not something Commons could show. Jev's question asks the same about "a row of pictures from Wikimedia Commons".
Both are told to judge the kind of subject, not the particular pictures returned. So the gate measures image
need together with whether the subject is the kind Commons covers. It does not measure need independently of
the provider. Whether Commons has suitable pictures for a particular query is left to the relevance step and its
minimum. Another image provider would need its own evaluation.

### Setup

- **Candidates:** each query's top 20 Commons results, without the images whose Jev NSFW score is 1.0 or more
  (the threshold from the #610 benchmark). This keeps 15,846 of 16,512 images.
- **Jev questions, in separate mode:**
  - image need, asked once per query with its web top ten's titles;
  - image relevance, one request per query covering all its candidates (946 queries, 16.8 images per request
    on average). Relevance latency was 0.34 s at p50, 0.47 s at p90, 0.56 s at p95 and 0.99 s at worst.
- **Reference labels** came from Sonnet judges, with rubrics written separately from the Jev questions:
  - **Need (0–3):** would a strip of three to five Commons pictures improve this page for a UK searcher? The
    judge saw the web top ten and the titles of the top five candidates. All 849 queries were labelled; 200
    (23.6%) scored 2 or more.
  - **Relevance (0–3):** the judge read each image's file name, description (first 400 characters) and
    categories, plus the query's first three web titles. It labelled the top 12 candidates of 150 queries.
    That makes 1,689 images. The queries are a stratified sample: 30 from each fifth of Jev need. Only the 682
    train queries with at least 3 candidates were eligible. The fifths are equal-sized (136 or 137 eligible
    queries each), so the sample is proportional to the eligible queries and needs no reweighting. It does not
    cover the 167 train queries with fewer than 3 candidates.
  - **Vision spot-check:** 100 of those images, chosen only from ones every local safety layer passed (rules,
    NudeNet and SigLIP2). The judge labelled them again, this time seeing the 320px thumbnail as well as the
    text. The thumbnails were deleted afterwards.
- **Label checks:** every id was labelled exactly once. 718 of the 849 need reasons are distinct. 40 of the 150
  sampled queries have all 12 images at 0, and spot checks show these are genuine. For example, the Commons
  results for "cardiff" are pictures of Quebec, and "alcaraz" returns the Spanish town, not the tennis player.

### Gate

The table scores each signal against a reference need of 2 or more. For thresholded signals, the precision and
recall are out of fold: each of 5 folds picks its F1-best threshold on the other four.

| Gate | AUC | Gated in | Precision | Recall |
|---|---|---|---|---|
| Always | 0.500 | 100.0% | 0.236 | 1.000 |
| Never | 0.500 | 0.0% | – | 0.000 |
| **Jev need** | **0.890** | 25.6% | **0.636** | 0.690 |
| Jev-safe candidate count | 0.644 | 73.4% | 0.283 | 0.880 |
| Pass-1 intent entity/local | 0.699 | 41.1% | 0.410 | 0.715 |

- **Intervals:** 95% intervals from 2,000 resamples of the queries with replacement. Jev need's AUC is
  [0.868, 0.913] and the intent baseline's is [0.664, 0.733]. The paired difference is [0.155, 0.229].
- **Threshold:** fitted on all train queries, the threshold is Jev need ≥ **1.63**. It gates in 28% of train
  queries and 27% of en-gb queries, with precision 0.62 and recall 0.75 in sample.
- **False positives** are mostly shopping queries, such as "barcelona kit 25/26", "walnut sideboard" and
  "holiday dresses". Jev wants pictures for these, but the rubric says Commons has little for products.
- **False negatives** are mostly places asked about in a transactional way, such as "windsor castle tickets",
  "stansted express" and "maps liverpool", plus bare city names like "liverpool" and "newcastle".

### Image ranking

NDCG@k uses gain 2^label − 1, computed over each sampled query's 12 labelled candidates. The 40 queries whose
labels are all 0 have no ideal ordering and are left out, so the means are over the other 110. They are means
over the stratified sample above, so they describe eligible train queries, not all queries.

| Order | NDCG@1 | NDCG@3 | NDCG@5 |
|---|---|---|---|
| **Jev relevance** | **0.871** | **0.885** | **0.901** |
| Commons rank | 0.614 | 0.648 | 0.676 |
| Mix, Jev − w·(Commons rank − 1), 5-fold CV | 0.860 | 0.874 | 0.893 |

- **Intervals:** query-resampled 95% intervals, as for the gate. For Jev relevance they are [0.816, 0.919] at
  NDCG@1, [0.849, 0.918] at NDCG@3 and [0.869, 0.929] at NDCG@5. For Commons rank they are [0.529, 0.687],
  [0.586, 0.707] and [0.619, 0.729]. The paired difference is [0.168, 0.349], [0.173, 0.303] and
  [0.169, 0.283].
- **Mix:** the folds chose w = 0, 0, 0, 0.02 and 0.05. Commons' order adds nothing on top of Jev.
- **Caveat:** this ranks only the labelled top 12. In production Jev would rank all the candidates.

The table below compares the text labels and Jev with the vision labels, on 100 images. Jev's score is rounded
for the exact and ±1 columns.

| Against vision | Spearman | Exact | ±1 |
|---|---|---|---|
| Text label | 0.897 | 77% | 99% |
| Jev relevance | 0.842 | 57% | 98% |

The text labels track what the pictures show closely. The reference therefore holds without vision, and so does
Jev's text-only relevance. Two caveats: the vision judge saw the same text as well as the picture, so the two
labels are not independent, and the 100 images all passed every local safety layer, so they are not a random
draw from the candidates.

### Number of images and minimum relevance

The table covers the 41 sampled queries that the recommended gate (Jev need ≥ 1.63) lets in. The pool is each
query's top k by Jev relevance, keeping only images at or above the minimum. "Good" and "Wrong" are shares of the
images shown.

| k | Min Jev relevance | Images per query | Queries with any | Good (≥2) | Wrong (0) |
|---|---|---|---|---|---|
| 4 | 0.0 | 3.98 | 100% | 66.9% | 14.1% |
| 4 | 1.0 | 3.22 | 88% | 80.3% | 2.3% |
| **4** | **1.5** | **2.83** | **80%** | **87.1%** | **0.9%** |
| 4 | 2.0 | 2.46 | 73% | 90.1% | 1.0% |
| 4 | 2.5 | 1.88 | 61% | 94.8% | 0.0% |

- **Other values of k:** at k = 3 and k = 6, the good share is within a point of k = 4 at each minimum of 1.0
  or more. With no minimum, k = 6 shows more wrong images (17.5% against 14.1%).
- **In sample:** k, the minimum and the gate threshold were all chosen on these same queries, so these shares
  are optimistic. They are exploratory estimates, not a measure of production quality. They rest on 41 queries
  and about 116 images. Resampling the queries puts the 87.1% good share at [75.6%, 96.3%] and the 0.9% wrong
  share at [0%, 2.9%].
- **Reference-wanted queries:** on the 34 sampled queries whose reference need is 2 or more, k = 4 with a
  minimum of 1.5 shows 3.26 images. 88% of them are good and 0.9% are wrong.

### Recommendation

These are the settings to carry into the holistic comparison, not settled production values. Only the gate's
precision and recall and the mix row above are out of fold. The threshold, k and minimum were all chosen in
sample. The holistic comparison (#615) tests them on the 295 en-gb queries, which played no part in choosing
them.

- **Gate:** show images when Jev image need ≥ **1.63**. That is about 27% of queries, and AUC is 0.89 against
  0.70 for the best non-Jev baseline. The gate is specific to Commons, as explained above.
- **Order:** sort by Jev image relevance alone, ignoring Commons' order.
- **Number:** show up to **4** images.
- **Minimum:** require Jev image relevance ≥ **1.5**. A fifth of gated-in queries then show no images, which
  is better than showing wrong ones.

These settings are in `devdata/seed_images/gate/recommended.json` for the holistic comparison.

## Holistic comparison

The deciding step compares whole result pages with the holistic judge (`holistic_judge.py`), on the 295 en-gb
queries, which none of the settings above were tuned on. These are the held-out queries of the earlier Combined
Search evaluations (`combined-search-haiku-eval.md`). They come from UK autocomplete, so their stored Staan lists
were fetched with `market=en-gb`, and the judge is told the searcher is in the UK. Locale changes the web results
more than the pictures: Commons search doesn't depend on it, and the gate lets in 27% of these queries against
28% of the train queries, so the conclusions should carry over to `en-us`. `mwmbl/rankeval/seed_images/holistic_arms.py` builds the arms and their
batches under `devdata/seed_images/holistic/`.

- **A0:** production's order, Jev composite + Staan rank.
- **A2, the recommended design:** when Jev need ≥ 1.63, a strip of up to 4 Jev-safe images with Jev relevance
  ≥ 1.5, highest first, is one extra entry after the top web result. It displaces no web result. 80 queries are
  gated in and 59 get a strip, of 213 images in all.
- **A1, no gate:** the two most relevant Jev-safe images join the web results as single results, merged by Jev
  composite, and the list is cut back to ten. An image scores as a substantive page Staan didn't return, so its
  composite is its Jev relevance − 0.5. This puts 191 images into 118 queries' top tens, mostly at positions 7
  to 10.

The judge sees a strip as `[IMAGES from Wikimedia Commons] 1. <file name> — <description> | 2. …` and a single
image as `[IMAGE] <file name> — <description>`, with descriptions cut at 150 characters. Since these runs, the
judge cuts descriptions at a sentence end or word boundary instead of mid-word, and strips encoded tags from titles
and descriptions. The runs weren't judged again: the same queries are compared, no title or tag changes on this
data, and only the shortening differs, in 19 of A2's 59 strips and 37 of A1's 191 images. Batches that show images
add a paragraph telling the judge that pictures may appear and to judge whether they help the query; the rest of
the rubric is unchanged. A vision check repeats 20 of the A2-vs-A0 comparisons with the strips' thumbnails, using
only strips whose every image passed the rules, NudeNet and SigLIP2 (26 of the 59 did). The thumbnails were
deleted after judging.

### Results

Haiku 4.5 judged each comparison in both orders. Only queries whose lists differ are compared. Preference runs
from −3 to 3, and a positive value favours the image arm.

**Read A2's result as "the strips are mostly relevant", not as "a better page".** A2's lists are A0's with one
entry added and nothing displaced, so the judge can't see any cost to showing a strip except a little scrolling.
The prompt also tells it a strip "adds to the web results rather than replacing one". Of the 118 A2 verdicts' notes,
99 say a list "adds" pictures and 94 point out that the web results are the same. Of the 104 verdicts for A2, 93
are "slight" and none "strong". +0.84 is therefore close to the share of strips the judge found relevant, less
those it found wrong. It doesn't measure what a strip costs in screen space or attention. The control arm,
A2-displace, makes the strip pay for itself. It is A2 with the strip in place of the tenth web result, so the
list stays at ten. Its batches also drop the clause saying a strip adds to the results; the prompt is otherwise
the same. On 4 of the 59 queries there are fewer than ten web results, so there the strip displaces nothing.
The comparisons are also conditional: only the 59 of 295 queries (20%) that get a strip are compared. Over all
en-gb queries the mean preference is about +0.17 for A2 and +0.15 for A2-displace.

| Comparison | Queries | Preference [95% CI] | Images / A0 / tie | Orders agree |
|---|---|---|---|---|
| **A2 vs A0** | 59 | **+0.84 [+0.64, +1.01]** | 85% / 8% / 7% | 93% |
| **A2-displace vs A0** | 59 | **+0.75 [+0.55, +0.92]** | 80% / 12% / 8% | 80% |
| A1 vs A0 | 118 | +0.12 [−0.03, +0.29] | 47% / 38% / 15% | 73% |
| A2 vs A0, strip passed every local layer | 26 | +0.85 [+0.63, +1.02] | 85% / 4% / 12% | 88% |
| A2 vs A0, strip flagged by a local layer | 33 | +0.83 [+0.52, +1.11] | 85% / 12% / 3% | 97% |
| A2 vs A0, vision subset, text only | 20 | +0.82 [+0.57, +1.02] | 85% / 5% / 10% | 90% |
| A2 vs A0, vision subset, with thumbnails | 20 | +1.27 [+0.93, +1.57] | 90% / 5% / 5% | 85% |

- **A2** wins clearly. Most verdicts are "slight", with 13 of 118 "clear", and the main reasons are relevance (83)
  and coverage (21). A0 wins on 5 queries, all where the strip shows the wrong sense of the query. Examples are
  other people with the same name ("boone helm", "tom wagner", "isak") and the palace for "versailles series".
  This is the same homonym failure the relevance labels showed.
- **A2-displace** keeps nearly all of A2's win when the strip costs the tenth web result: +0.75 against +0.84.
  - **Per query:** it scores 0.09 lower than A2 on average, with a 95% bootstrap interval of [−0.22, +0.03].
    The two agree in direction on 49 of the 59 queries, and their per-query preferences correlate at 0.79.
  - **Reasons:** relevance 72, coverage 25 and junk 7.
  - **Flags:** A0's displaced tenth results were weak, so A0 collects more thin (17 against 7), duplicate (15
    against 7) and poor-extract (18 against 11) flags.
  - **Losses:** A0 wins 7 queries. Five of them are A2's wrong-sense strips. The other two are "kharg island" and
    "minecraft movie" (a Spanish logo), where the displaced result was worth more than the pictures.
  - **What this shows:** the strip's win is not just added content. Relevant pictures beat a tenth web result.
- **A1** is not significantly better than A0, and judges disagree more often (73% order agreement). It has no
  gate and no minimum relevance, so its images often land in the last few places as off-topic pictures (off-topic
  flags: 105 against A0's 72). A0 wins 45 of the 118 queries, for example "chargeback" and "willis pharmacy".
  Where A1 wins, it is mostly because the image replaced a weak tenth result.
- **Local safety layers:** strips the local layers flagged score the same as those they passed. The local layers
  don't pick out worse strips, so they matter for safety only.
- **Vision:** with thumbnails, the judges favour A2 more strongly (+1.27 against +0.82 from text alone on the
  same 20 queries, with overlapping intervals). The two agree in direction on 19 of the 20 queries, so the
  text-only judgment doesn't overstate A2. Seeing the pictures may also simply make them more salient to the judge.
- **A1 against A2:** A1 differs from A2 in five ways at once: no gate, no minimum relevance, single results
  rather than a strip, low positions, and displacing web results. On the 44 queries both arms change, A1 scores
  +0.28 and A2 +0.82. A1 is therefore a weak test of single images as such. A2-displace shows that losing
  one web result costs little, so most of the gap comes from A1's other differences.
- **Wrong-sense strips:** the judges do penalise bad pictures. All five of A0's wins are wrong-sense strips.

### Recommendation

Ship option 2, the gated strip:

- **Gate:** show a strip only when Jev image need is at least 1.63, which is about 27% of queries.
- **Images:** take the Jev-safe images (Jev NSFW below 1.0) with Jev image relevance of at least 1.5. Sort them
  by Jev relevance alone and show at most 4.
- **Placement:** put the strip after the top web result. Judged holistically, it wins whether it is an extra
  entry (+0.84 on the gated-in queries, about +0.17 over all en-gb queries) or takes the tenth web result's place
  (+0.75, about +0.15 over all en-gb queries). Screen space is therefore no reason to hold the strip back.

Don't merge single images into the web ranking (option 1) as built here: judged holistically it is no better
than today's results, though that version had no gate or minimum relevance either. Wrong-sense images are the main remaining failure, so the next lever is the minimum relevance or a
check that the image's subject matches the top web result's.

Before shipping:

- **Wikipedia card:** when the top result is an English Wikipedia page, the front end shows it as a card with the
  article's lead image (`WikipediaCard.svelte`). That is the case for 37 of the 59 queries with a strip, so the
  strip would sit right under a card that may already show one of its pictures. The judge saw only the card's
  title, URL and extract. Drop from the strip any image that is the card's lead image, matched by Commons file
  name, and judge the card and strip together before deciding whether the strip still helps there.
- **Escaping:** Commons titles and descriptions are user-entered. The judge read them as plain text, but the
  front end must render them as escaped text, never as HTML.
