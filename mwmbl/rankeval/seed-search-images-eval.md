# Images in Seed Search: evaluation

Issue #610 proposes adding Wikimedia Commons images to Seed Search. Jev, which already ranks the web results, would
judge from each image's text (file name, description, categories) whether the image shows nudity. Only images Jev
passes would go into the index, and they would be served from there without another check. This document evaluates
that plan with the harness in `mwmbl/rankeval/seed_images/`, and the Summary at the end collects its decisions.

## Safety

The scripts are in `safety.py`, and their outputs are in `devdata/seed_images/safety/`.

### 1. Separate or combined request?

*Separate* sends only the NSFW questions. *Combined* adds them to the request Seed Search already sends: the web
pool's relevance and quality questions (Staan's top ten, then the LTR's top 30, about 40 results), plus image
relevance and image need. I scored the safety benchmark's 1,351 images both ways, 20 to a request and shuffled
across tiers. Each combined request used a random train query and that query's pool.

| Threshold | Mode | Art (52) | Rule-evading art (22) | Photos (331) | Rule-evading photos (29) | Safe excluded (438) | Medical | Cleavage | Borderline |
|---|---|---|---|---|---|---|---|---|---|
| 0.5 | separate | 96% | 20 | 98.8% | 25 | 11.2% | 98% | 98% | 83% |
| 0.5 | combined | 96% | 20 | 99.1% | 26 | 13.5% | 100% | 98% | 85% |
| **1.0** | **separate** | **94%** | **19** | **98.5%** | **25** | **3.4%** | 97% | 80% | 61% |
| 1.0 | combined | 96% | 20 | 98.5% | 25 | 4.1% | 97% | 85% | 66% |
| 1.5 | separate | 94% | 19 | 97.3% | 25 | 1.8% | 95% | 53% | 46% |
| 1.5 | combined | 94% | 19 | 98.5% | 25 | 1.8% | 97% | 68% | 55% |
| 2.0 | separate | 83% | 13 | 91.8% | 19 | 0.0% | 87% | 18% | 17% |
| 2.0 | combined | 75% | 9 | 94.9% | 19 | 0.0% | 88% | 35% | 17% |

- **The two modes rank the benchmark images almost the same way.** AUC for art vs safe is 0.987 separate and 0.986
  combined, for rule-evading art 0.970 and 0.967, and for photos 0.995 and 0.996. The scores correlate at 0.995.
  Between 25 and 47 of the 1,351 verdicts flip, depending on the threshold. The combined scores are about 0.03
  higher on average. For scale, between two separate runs (the run in the issue comment and this one), 11 to 32
  verdicts flip.
- **On real queries, the combined request passes more images.** I asked about the top 20 candidates for 200 train
  queries (3,397 images) in combined mode and compared the scores with separate mode. They correlate at 0.968. At
  1.0, combined mode passes 25 images that separate mode flags and flags 9 that it passes. At 1.5 it passes 29 and
  flags 4. In these requests the query matches the images, and Jev gives them the benefit of the doubt: Dürer's
  *Christ Washing the Feet* for "5.6 feet in cm", a Silenus relief for "gladiator", and an 1821 Mysore painting that
  the deny rules and SigLIP2 both flag.
- **Speed is not the deciding factor.** These are latencies over 200 requests, four at a time, against the 1.5 s
  timeout:

| Request | p50 | p95 | Over 1.5 s | Questions | Input tokens | Output tokens |
|---|---|---|---|---|---|---|
| Web only (today) | 0.41 s | 0.61 s | 0% | 75 | 18,800 | 1,100 |
| Separate NSFW (about 17 images) | 0.33 s | 0.50 s | 0% | 17 | 6,400 | 250 |
| Combined | 0.51 s | 0.72 s | 0% | 110 | 31,700 | 1,600 |

**Use a separate request.** An indexed image is later served for other queries without any check, so its verdict has
to depend on the image alone. A combined verdict also depends on the query of whoever happened to search first. That
query can't be stored, for privacy, and the data above shows it makes Jev more lenient. A separate request, sent in
parallel with the web request, adds no latency, and its verdict can be cached per file.

### 2. Real queries: where Jev and the local layers disagree

These are separate-mode scores for every candidate of the 950 queries that have candidates, 16,512 images in all,
compared with the local layers: the deny rules, NudeNet, and SigLIP2 at 0.38. A layer flags an image if its verdict
isn't SAFE, as in the pipeline, so NudeNet's UNCERTAIN (a covered-body detection, 98 images) counts as a flag.

| Jev passes (score below) | Images passed | Any local layer flags | Rules | NudeNet | SigLIP2 | Queries affected |
|---|---|---|---|---|---|---|
| 0.5 | 14,597 (88.4%) | 3,044 | 103 | 299 | 2,769 | 741 |
| **1.0** | **15,846 (96.0%)** | **3,562** | 127 | 350 | 3,255 | 776 |
| 1.5 | 16,171 (97.9%) | 3,761 | 146 | 380 | 3,445 | 781 |

I classified the disagreements by reading their text. I didn't look at any of the images.

- **SigLIP2 accounts for almost all of the disagreement.** It flags 22% of all real candidates, mostly by mistake:
  planets, fruit, bacteria, logos, cars, footballers and celebrity portraits. I read a random sample of 40 images
  that Jev scores below 0.5 and that a local layer flags, and all 40 were clearly false positives. In a sample of 40
  that Jev scores between 0.5 and 1.0, about 35 are clothed people or posters. About 5 might be revealing (a "cupcake"
  promo, a 1934 film poster, a Leyendecker Baby New Year), and none read as nude.
- **The rules flags that Jev passes are mostly the deny list matching innocent words.** Examples are insects and
  birds ("Brachycera sex", "Lilac-breasted roller"), book scans that mention sex, and knots filed under "BDSM knots".
  Most exceptions score between 1.0 and 1.5, so a threshold of 1.0 excludes them: ten pages of a Hiroshige album
  categorised "Eroticism in the British Museum" (1.43–1.49), Debret's *Caboclo* (1.49), and two of four 1820s Mysore
  paintings whose description mentions erotics (1.05, 1.19). The other two pass at 1.0 (0.52, 0.94), as does a porn
  actress at an adult expo (0.80), dressed according to her categories.
- **The NudeNet flags that Jev passes are mostly round or skin-coloured objects:** planets, fruit, pills and skin
  close-ups. A few may be revealing: Cape Verde carnival dancers (0.96–0.99), a *Blazing Saddles* publicity still
  (0.78) and a magazine cover (0.73). The covered-body detections add 32 below 1.0, all clothed people by their text.
- **Jev also flags images that every local layer passes:** 62 at 1.5 or above (54 distinct files). Most are classical
  art that the pixel models miss: the Pietà, *Venus Carrying off Ascanius*, Canova, the Versailles statues, the
  crucifixions, and a centaur mosaic. There are also medical diagrams and MRI scans, and a photo of a toddler on a
  potty.

### 3. Sensitive probe queries

These are 55 borderline queries, with 1,095 candidates in all: classical art, swimwear, sport, the body, medicine,
seven explicit queries and five innocent controls. Each image was scored by the local layers and by Jev in separate
mode.

| Jev passes (score below) | Images passed | Any local layer flags | Rules | NudeNet | SigLIP2 | Queries affected |
|---|---|---|---|---|---|---|
| 0.5 | 415 (38%) | 99 | 8 | 16 | 89 | 35 |
| **1.0** | **516 (47%)** | **148** | 13 | 18 | 133 | 43 |
| 1.5 | 602 (55%) | 211 | 18 | 30 | 194 | 45 |

- **Between 1.0 and 1.5, Jev passes images that shouldn't be shown.** Below 1.5 it passes John Webber's *Poedua*, a
  portrait of a topless Tahitian woman (1.17, flagged by NudeNet and SigLIP2). It also passes *Caboclo* again (1.17),
  "Bikini Model Jassi" in "Fetish models" (1.35–1.42), Rubens's *Coronation of the Hero of Virtue* (1.35), Max
  Weber's *Burlesque* (1.48), and bikini-contest, pole-dance and sumo photos.
- **Below 1.0, what passes doesn't read as nude.** It is swimwear (a bikini contest at 0.97, a 1930s bathing-suit
  tableau), a pulp cover (0.71), Rubens's *Christ and the Repentant Sinners* (0.65), and many false positives, such as
  the planet Venus for "venus de milo". None of the actual statues for "statue of david", "venus de milo", "greek
  statue", "birth of venus", "odalisque" or "the three graces" pass.
- **Explicit queries are almost entirely filtered.** At 1.0, 0 of 20 candidates pass for "nude" and 0 of 20 for
  "topless beach". For "naked woman", 4 pass, all of them astronomy. For "erotic art", 2 pass: Klimt's *The Kiss* and
  the outside of a museum. For "skinny dipping", "titian venus of urbino" and "odalisque", 0 pass.
- **Controls are untouched.** All 100 candidates for red panda, eiffel tower, bicycle, chocolate cake and volcano pass.

### Recommendation

- **Request:** a separate NSFW request, as argued in section 1.
- **Threshold: pass below 1.0, for both showing and indexing.** It catches 94% of art nudes and 98.5% of nude photos
  on the benchmark, excludes 3.4% of safe benchmark images, and drops 4.0% of real candidates. The images Jev
  passes between 1.0 and 1.5 include an erotic album, a topless portrait and a "fetish model". Dropping the threshold
  to 0.5 would cost another 7.6% of real candidates, and in my samples it removes revealing images but no nude
  ones.
- **Query gate:** keep `rules.query_verdict` in front of image search. Jev already filters nearly every candidate
  for explicit queries, so the gate is defence in depth, and it saves a request whose images would be mostly
  off-topic leftovers. A Jev question about the query isn't needed. The gate does refuse "breastfeeding" and "breast
  cancer", which is a cost we can accept. It misses "naturism": the rules have `naturist\w*` but not `naturism`.
  For "naturism", Jev passed only a road sign.

### Caveats

- **No ground truth for real images.** No one viewed a flagged image. The disagreements are classified from text, and
  "doesn't read as nude" is not "not nude". With no pixel check behind Jev, images with bland text are the residual
  risk. Section 2's 62 Jev-only catches show the reverse also happens: the pixel layers miss art that Jev catches.
- **The local layers are not ground truth either.** SigLIP2 at 0.38 flags 22% of real images, mostly in error.
- **One run each.** Two separate runs already differ by 11 to 32 verdicts out of 1,351, so the small differences
  between modes on the benchmark are within noise. On real queries the difference points one way at 1.0 and 1.5
  (25 against 9, 29 against 4), though not at 0.5, where 60 go one way and 55 the other.
- **Benchmark caveats from the issue still apply:** the nude tiers were sampled from nudity categories, the
  rule-evading groups are small (22 and 29), and the safe set wasn't hand-checked.
- **Pool approximation:** the web pool is rebuilt from `pass2_pool.jsonl`, which is not a live serving pool. The
  latencies come from one machine, four requests at a time, without reused connections (the harness's, for separate).

### Residual risks of serving indexed images without a check

- **Known misses.** At 1.0, Jev passes 3 of 52 art nudes and 5 of 331 nude photos, including 7 of 51 rule-evading.
  Each indexed miss is served to everyone whose query matches it.
- **Bland text.** A nude named `IMG_1234.jpg` with no description or categories can't be caught from text. New,
  uncategorised uploads are the riskiest; a minimum file age or some categories would reduce this.
- **Prompt injection.** Anyone can edit a description, and Jev reads it: "no people are shown" may steer it. Untested.
  Production must tell Jev that the file name, description and categories are untrusted uploader text, never
  instructions to follow, and re-check the NSFW threshold on the benchmark with that prompt.
- **Changes after approval.** An overwritten file serves new pixels at the same thumbnail URL. Recheck each indexed
  file's SHA-1 and text, re-ask Jev on a change, drop deleted files, re-run the benchmark when Jev's model changes.

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
  and about 116 images. Resampling the queries puts the 87.1% good share at [75.6%, 96.3%]; that covers sampling
  noise only, not the optimism of choosing the settings here. The 0.9% wrong share is one image, too few to
  resample: its exact 95% upper bound is about 5%.
- **Reference-wanted queries:** on the 34 sampled queries whose reference need is 2 or more, k = 4 with a
  minimum of 1.5 shows 3.26 images. 88% of them are good and 0.9% are wrong.

### Recommendation

These are the settings to carry into the holistic comparison, not settled production values. Only the gate's
precision and recall and the mix row above are out of fold. The threshold, k and minimum were all chosen in
sample. The [holistic comparison](#holistic-comparison) tests them on the 295 en-gb queries, which played no part in choosing
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
Search evaluations (`combined-search-haiku-eval.md`), which come from UK autocomplete. That evaluation has en-us
lists for the same queries too; this comparison uses its en-gb run, whose Staan lists were fetched with
`market=en-gb`, and the judge is told the searcher is in the UK. Locale changes the web results more than the
pictures: Commons search doesn't depend on it, and the gate lets in 27% of these queries against 28% of the train
queries, so the conclusions probably carry over to `en-us`. The en-us lists would let that be checked.
`mwmbl/rankeval/seed_images/holistic_arms.py` builds the arms and their batches under
`devdata/seed_images/holistic/`.

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
judge's `commons_text` decodes entities and strips tags, encoded ones too, from titles as well as descriptions, and
descriptions are cut at a sentence end or word boundary instead of mid-word. The runs weren't judged again: the
same queries are compared, no title or description in this data contains a tag or entity, and only the shortening
differs, in 19 of A2's 59 strips and 37 of A1's 191 images. Batches that show images
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
than today's results, though that version had no gate or minimum relevance either.

Wrong-sense images are the main remaining failure. Jev's image-relevance question sees only the query; giving it
the top web results should catch most of them (see the Summary's production list).

Before shipping:

- **Wikipedia card:** when the top result is an English Wikipedia page, the front end shows it as a card with the
  article's lead image (`WikipediaCard.svelte`). That is the case for 37 of the 59 queries with a strip, so the
  strip would sit right under a card that may already show one of its pictures. The judge saw only the card's
  title, URL and extract. Drop from the strip any image that is the card's lead image, matched by file name: the
  last segment of the summary's `originalimage` URL, URL-decoded, with underscores read as spaces. That may be a
  non-free file local to en.wikipedia, which no Commons image matches. Then judge the card and strip together
  before deciding whether the strip still helps there.
- **Escaping:** Commons titles and descriptions are user-entered. The judge read them as plain text, but the
  front end must render them as escaped text, never as HTML.

## Indexing

#610 proposes writing the Jev-approved images into the main index, so that plain standard search
(`/api/v2/search/`, no Jev at query time) shows them to everyone. This section measures what that does.
The code is `mwmbl/rankeval/seed_images/indexing.py`, and the data is under `devdata/seed_images/indexing/`.

### Setup

- **Images:** the Jev-safe candidates (NSFW below 1.0) of the 849 train queries, each once: 11,652 images. Nothing
  else filters them.
- **Documents:** each image is a page of its own. The URL is its Commons file page (`…/wiki/File:X.jpg`), which
  is enough to tell an image from a web page. The title is the file name without its extension. The extract is
  the description, optionally followed by the categories. Both are cut to the crawler's 65 and 155 characters.
  The image goes through `index_documents`, under its own terms like any crawled page, never under the query
  that found it.
- **Indexes:** all writes go to copies.
  - The en-gb index (`index_write_order/heuristic.tinysearch`) holds the 958 lookup terms of the en-gb queries,
    with about 44 documents per term, in production's heuristic write order. Its term pages are full like
    production's, but it has only 65,536 pages to production's 102M. So the images' 130k other terms land on
    the queried terms' pages far more often than they would in production.
  - **Same-term arms** therefore write an image only to the pages of terms the index already holds, which is
    the competition production would see. The full-write arms are an upper bound on eviction.
  - The dev index (`index-v2.tinysearch`) is what dev search reads. It holds 1.1 documents per term on 2,560
    pages, and 293 of the 295 en-gb queries get fewer than ten results from it. It is too thin to be a fair
    test, so it is reported only for completeness.
- **Search:** standard search's ranker, `MMRRanker(LTRRanker)`, run on the 295 en-gb queries. Each query's
  Wikipedia results are fetched once and shared by every arm. With `PYTHONHASHSEED` random, tied results swap
  places between runs and change 34 of 295 top tens, so every run uses seed 0.
- **Relevance:** each image shown is scored with Jev image relevance. The gate's score is used where that
  (query, image) pair has one; otherwise the harness asks the same question, and the answer is cached.

### Eviction

| Index, arm | Image term docs stored | Existing docs evicted | Evicted by images of the same term |
|---|---|---|---|
| en-gb, description, full write | 241,964 | 3,087 of 39,315 (7.9%), 579 terms | 11 |
| en-gb, + categories, full write | 263,382 | 4,013 of 39,665 (10.1%), 599 terms | 15 |
| en-gb, description, same term | 109, on 59 terms | 5 | 5 |
| en-gb, + categories, same term | 74, on 28 terms | 6 | 5 |
| dev, description, full write | 87,466 | 85,162 of 90,690 (94%) | 129 |

- **Same-term competition barely evicts anything.** The heuristic write order gives `commons.wikimedia.org`
  no domain score, so an image loses to every existing document on a full term page. Of the 36,552 image
  documents sent to the queried terms' pages, only 109 fit.
- **Nearly all eviction is from page sharing.** When a page receives documents for a new term, each new term's
  best document goes ahead of the existing terms' lower-ranked ones. That costs 7.9% of this test index. In
  production it would scale with how often an image term shares a page with a queried term, which is much
  rarer. The dev index's 94% is the same effect on 2,560 pages.

### What standard search shows

| Index, arm | Top tens changed | Changed with no image shown | Queries with images | Images | Jev rel ≥ 1.5 | Jev rel < 1 | On gate-rejected queries | Web results displaced (by images) |
|---|---|---|---|---|---|---|---|---|
| en-gb, description, full | 106 | 92 | 14 | 31 | 12 | 19 | 30 | 219 (35) |
| en-gb, + categories, full | 112 | 97 | 15 | 30 | 11 | 18 | 28 | 258 (36) |
| **en-gb, description, same term** | **11** | 5 | **6** | **14** | **12** | **2** | **13** | 19 (14) |
| en-gb, + categories, same term | 10 | 4 | 6 | 14 | 12 | 2 | 13 | 18 (14) |
| dev, description, full | 40 | 13 | 27 | 31 | 10 | 15 | 23 | 26 (2) |

- **Same-term arm, the closest to production:**
  - The images are "euro 2025" (6 images, at positions 2 and 4–7 and 10), "matalan" (4 store photos, one at
    #1), "glasgow airport", "noel edmonds age", "italy vs bosnia" (a rugby photo, Jev relevance 0.5) and "cast of
    wednesday" (a Sweaty Betty shop front that matched "of wednesday", Jev relevance 0.02).
  - The 14 web results they pushed out are mostly tag and listing pages, such as dailycannon `/tag/euro-2025/`
    and pinterest's matalan board, but they include BBC Sport's Euro 2025 live page.
- **Full-write arms:**
  - These changes are mostly evictions: 92 of the 106 changed top tens show no image.
  - 17 of the 31 images came in through completion terms, such as "golden" for "price of gold" (a golden
    retriever) and "outside" for "out". This test index has nothing on those terms' pages, so they show how
    images fill any thin term: almost all of them are irrelevant (Jev relevance below 0.1).
- **The gate doesn't apply:** 13 of the 14 same-term images, and 30 of 31 in the full write, are on queries Jev
  need would reject (below 1.63), mostly navigational ones such as "matalan". Nothing caps how many images one
  list gets.

### Should categories be indexed?

The probe tokenised every image with the indexer's own `tokenize_document`.

- **All images share some terms.** Every image is indexed under `commons`, `wikimedia`, `wiki` and `org`, from
  its URL, just as every Wikipedia page is under its own. 4,153 are also under `jpg`.
- **Categories rarely reach the index.** The extract is cut to 155 characters, and only its first 10 tokens and
  10 bigrams are indexed, so categories add terms for only 4,962 of the 11,652 images (3.7 terms each).
- **What they add is mostly maintenance and licence text:** `self-published work` (721 images), `images from`
  geograph (599), `taken with` (424), `cc-zero` (329), `flickr images` (225), `canon` (109), `nikon` (69) and
  `media needing categories` (9). Long licence tags such as `CC-BY-SA-4.0` never get past the cut.
- **On the search numbers above,** categories are no better on any of them: slightly more evictions, fewer
  images on their own terms, and the same shown images.
- **Decision:** drop them. Index the file name and description only.

### Holistic comparison

Only the en-gb same-term arm was judged, because it is the closest to production. The full-write and dev arms
weren't judged: their changes are dominated by the small test indexes' eviction artefacts (92 of the 106
full-write top tens that changed show no image), so a verdict would mostly measure page sharing. The batches
compare the image-augmented lists (X) with the original ones (Y). Images are shown as
`[IMAGE] <file name> — <description>`, and the work is in `engb/description_same_term_vs_original/`.

| Comparison | Queries | Preference for X (95% CI) | X / Y / tie | Orders agree |
|---|---|---|---|---|
| Same term vs original | 11 | −0.27 [−0.55, +0.00] | 9% / 36% / 55% | 64% |

- **n = 11 is tiny.** This is a direction, not a measurement.
- **No query wins in both orders,** and six of the eleven are ties.
  - "matalan" loses in both, on top result: a store photo at #1 pushed the brand's pages down.
  - "italy vs bosnia" (a rugby photo) and "cast of wednesday" (a shop front) lose on junk and relevance.
  - "euro 2025" splits, relevance for X against redundancy, with 6 of its 10 places taken by pictures.
    "glasgow airport" splits too.

### Recommendation

These runs use today's LTR, never trained on image pages; retraining it with image results should fix the ranking
losses (store photo at #1, junk images). Indexed images rarely evict anything at production's page count. Then:

- **Text:** index the file name and description only, without categories.
- **Cap:** allow at most one or two images per standard-search list, so a query like "euro 2025" can't fill
  with pictures.
- **Gate-rejected queries:** decide whether standard search should show images on queries Jev need would
  reject. Nearly all the images it surfaced were on such queries, and the losses ("matalan" at #1) are
  navigational ones. Standard search has no Jev, so honouring the gate means storing something like the need
  score with the image, or not showing images in standard search at all.
- **Thin terms:** images reach thin terms easily, as the full-write arms' completion-term junk shows, so check
  against a production-sized index before rollout.

## Summary

- **Seed Search:** ship the gated strip, the holistic phase's A2.
  - **Gate:** show images when Jev image need ≥ 1.63, about 27% of queries.
  - **Images:** Jev-safe images (NSFW < 1.0), with Jev image relevance ≥ 1.5, sorted by Jev relevance, at
    most 4.
  - **Placement:** after the top web result. It beats today's page by +0.84 on gated-in queries (+0.75 when it
    displaces the tenth result), and its main failure is wrong-sense strips.
- **Safety:** show or index an image only if Jev's NSFW check, a separate request in parallel with the web
  request, passes it below 1.0, with no pixel check behind it. This replaces DistilBERT. Keep `rules.query_verdict`
  in front of image search, and add `naturism` to the rules. Residual risk: Jev reads only text, so a nude whose
  text says nothing can't be caught, and Jev passes some nudes even with their text (the Safety section lists them).
- **Standard search:** with today's LTR, never trained on image pages, indexed images judge slightly worse (−0.27,
  n = 11) on the arm closest to production; retraining the LTR with image results is expected to fix that. Index
  the file name and description only, cap images per list and settle the gate-rejected question.
- **Left for production:**
  - Build Seed Search's image request, run in parallel with Staan.
  - Serve the strip in the UI with attribution and licence, render Commons text escaped, and leave out the
    Wikipedia card's lead image (the holistic comparison's "Before shipping" list).
  - Tell Jev that Commons text is untrusted, and re-check the NSFW threshold with that prompt (Residual risks).
  - Set the Jev latency budget for the extra questions.
  - Disambiguate wrong-sense strips with the top web result. Jev's image-relevance question sees only the query,
    so give it the top result or two as well, and drop images whose subject doesn't match that meaning.
  - Indexing: retrain the LTR with image results, re-evaluate on a production-sized index; flag, cap, gate rule.
