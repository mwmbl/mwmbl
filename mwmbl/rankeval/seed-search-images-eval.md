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
