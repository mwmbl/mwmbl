# Images in Seed Search: evaluation

Issue #610 proposes adding Wikimedia Commons images to Seed Search. Jev, which already ranks the web results, would
judge from each image's text (file name, description, categories) whether the image shows nudity. Only images Jev
passes would go into the index, and they would be served from there without another check. This document evaluates
that plan with the harness in `mwmbl/rankeval/seed_images/`. This first section covers safety. The scripts are in
`safety.py`, and their outputs are in `devdata/seed_images/safety/`.

## Safety

**Recommendation:** ask about nudity in a separate request, sent in parallel with the web request. Pass an image
only if it scores below **1.0**, both for showing it and for indexing it. Keep the deny rules' query gate
(`rules.query_verdict`), and add `naturism` to it.

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
compared with the local layers: the deny rules, NudeNet, and SigLIP2 at 0.38. Ninety-eight thumbnails couldn't be
fetched, so the pixel layers didn't check them.

| Jev passes (score below) | Images passed | Any local layer flags | Rules | NudeNet | SigLIP2 | Queries affected |
|---|---|---|---|---|---|---|
| 0.5 | 14,597 (88.4%) | 3,016 | 103 | 232 | 2,769 | 739 |
| **1.0** | **15,846 (96.0%)** | **3,530** | 127 | 259 | 3,255 | 775 |
| 1.5 | 16,171 (97.9%) | 3,729 | 146 | 283 | 3,445 | 780 |

I classified the disagreements by reading their text. I didn't look at any of the images.

- **SigLIP2 accounts for almost all of the disagreement.** It flags 21% of all real candidates, mostly by mistake:
  planets, fruit, bacteria, logos, cars, footballers and celebrity portraits. I read a random sample of 40 images
  that Jev scores below 0.5 and that a local layer flags, and all 40 were clearly false positives. In a sample of 40
  that Jev scores between 0.5 and 1.0, about 35 are clothed people or posters. About 5 might be revealing (a "cupcake"
  promo, a 1934 film poster, a Leyendecker Baby New Year), and none read as nude.
- **The rules flags that Jev passes are mostly the deny list matching innocent words.** Examples are insects and
  birds ("Brachycera sex", "Lilac-breasted roller"), book scans that mention sex, and knots filed under "BDSM knots".
  The exceptions all score between 1.0 and 1.5, so a threshold of 1.0 excludes them: ten pages of a Hiroshige album
  categorised "Eroticism in the British Museum" (1.43–1.49), Debret's *Caboclo* (1.49), and the Mysore paintings
  (1.05–1.19).
- **The NudeNet flags that Jev passes are mostly round or skin-coloured objects:** planets, fruit, pills and skin
  close-ups. A few may be revealing: Cape Verde carnival dancers (0.96–0.99), a *Blazing Saddles* publicity still
  (0.78) and a magazine cover (0.73).
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
| 0.5 | 415 (38%) | 99 | 8 | 14 | 89 | 35 |
| **1.0** | **516 (47%)** | **148** | 13 | 15 | 133 | 43 |
| 1.5 | 602 (55%) | 211 | 18 | 20 | 194 | 45 |

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
- **The local layers are not ground truth either.** SigLIP2 at 0.38 flags 21% of real images, mostly in error.
- **One run each.** Two separate runs already differ by 11 to 32 verdicts out of 1,351, so the small differences
  between modes on the benchmark are within noise. The difference on real queries points one way, though: at 1.5,
  combined mode passes 29 images that separate mode flags and flags only 4 that it passes.
- **Benchmark caveats from the issue still apply:** the nude tiers were sampled from nudity categories, the
  rule-evading groups are small (22 and 29), and the safe set wasn't hand-checked.
- **Pool approximation:** the web pool is rebuilt from `pass2_pool.jsonl`, which is not a live serving pool. The
  latencies come from one machine, with four requests at a time and cached connections not reused.
