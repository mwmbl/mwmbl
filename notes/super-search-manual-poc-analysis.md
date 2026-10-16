# Super Search manual PoC — relevance analysis

Companion to `ss_manual_poc.md` (the raw Gold / Standard / Super Search panels).
10 train queries, seed 42, production-default SS config (no force-include, bandit off).
"off-gold" = a Super Search result whose domain is NOT in Google's gold set.
Relevance judged by reading the title+snippet against the query intent.

## Per-query judgments

### 1. england cricket
SS top result correct (Wikipedia England cricket team). Off-gold results: 1.
- #3 HN "Pitchers Adulterating Baseballs" (baseball, mentions cricket in passing) — **IRRELEVANT**.
- #5–7 1951/1954/1963 English cricket seasons are en.wikipedia (gold-domain), topically on-cricket but niche/historical vs Google's current-team/news intent.
Verdict: good top, 1 off-gold irrelevant.

### 2. lewis cope  ← worst case
SS top result correct. Off-gold: 7.
- #2 Wikimedia Commons category for Lewis Cope — relevant (different domain).
- #3,6,7,8,9,10 — **six archive.org junk items** matched on "Lewis"/"Cope" as separate tokens
  (Wendy Cope poems, "Lewis E 063" library manuscript, VOA Africa transcript, 18th-century
  studies, a CIA ballistic-missiles report). All **IRRELEVANT**.
Verdict: 1 relevant / 6 irrelevant off-gold. Clear ranking failure — homonym/token noise.

### 3. semenyo ghana
Only 2 SS results, both Wikipedia (Antoine Semenyo, Jai Semenyo), both relevant. Sparse but clean.

### 4. crash bandicoot  ← best "different-domain-but-relevant" case
Off-gold: 6, mostly relevant.
- #2,9,10 all-things-andy-gavin "Making Crash" (lead programmer's series) — relevant.
- #4 naughtydog.com (the developer) — relevant.
- #5 Ars Technica "How Crash hacked the PlayStation" — relevant.
- #6 Firefox addon "Crash Bandicoot" — marginal/irrelevant (browser theme).
Verdict: 5 relevant / 1 marginal off-gold. Strongly supports hypothesis 1.

### 5. mashle
Off-gold: 3, all relevant.
- #1 br.wikipedia (Breton), #3 Wikimedia Commons, #4 playstationcouch "Show HN Mashle opening".
Lots of foreign-lang/commons duplicates of the same content, but on-topic.

### 6. gallery london  ← intent/coverage mismatch
Google returns the actual gallery sites (nationalgallery.org.uk, saatchi, tate, serpentine…).
SS (and Standard) return Wikipedia articles + news ABOUT galleries. All 5 SS results are
topically relevant but NONE are the navigational results the user likely wants.
Standard search also 0/2 gold-domain here — this is an index-coverage gap, not an SS bug.

### 7. bed bug
Off-gold: 8, mixed.
- #2 itch.io game "Beloved Bed Bug" — **IRRELEVANT**.
- #3 minecraft.wiki "DoDaylightCycle bed bug" — **IRRELEVANT** (a Minecraft bug).
- #4 MDPI insecticide resistance, #5 dailycaller EPA, #6 hotel bed-bug blog, #7 Orkin cities,
  #8 Guardian, #10 HN — relevant.
Verdict: 6 relevant / 2 irrelevant off-gold.

### 8. tiramisu  ← homonym failure + intent mismatch
Google returns recipes (tastesbetterfromscratch, sallysbaking…). SS returns Wikipedia + homonyms.
Off-gold: 8.
- #1 az.wikipedia, #2 bar.wikipedia, #8 NPR "father of tiramisu died" — relevant.
- #4,9 tiramisu-compiler.org (a polyhedral compiler), #6 Meta VR codename, #7 Android 13
  codename, #5 quarter-mile — **IRRELEVANT** (software/product homonyms).
Verdict: 3 relevant / 5 irrelevant off-gold. No recipes surfaced at all.

### 9. empire biscuits
Only 1 SS result (Wikipedia), relevant. Google returns recipes; SS sparse but clean.

### 10. stoke city  ← intent skew (club vs city)
"stoke city" = the football club (Google returns all club sites). Off-gold: 9.
- #3,7,9,10 IMDb match pages (Liverpool vs Stoke etc.) — relevant to the club.
- #2 stoke.gov.uk, #4,6 gov.uk, #5 council webcasting, #8 Ofsted — relevant to *Stoke the place*,
  but not the football-club intent.
Verdict: broadly on-topic (~9), 0 truly irrelevant, but intent-skewed toward the city/council.

## Tally (off-gold SS results)

~47 off-gold results across the 10 queries:
- **~32 (~⅔) genuinely relevant** → supports "different-domain-but-relevant" (hypothesis 1)
- **~15 (~⅓) genuinely irrelevant** → supports "actually irrelevant" (hypothesis 2)

(Counts are judgment calls; e.g. stoke-council items counted relevant-to-topic though intent-skewed.)

## Mechanism

SS scores collapse after rank 1: the LTR re-ranker gives the top result a real score and
~0 to everything after (e.g. england cricket: 0.18, then 0.0007, 0.0, 0.0005…). Standard
search returns a tight 2–3 results; SS expands recall to fill 10 slots but the re-ranker
can't discriminate in the low-confidence pool, so homonym/token noise floats into ranks 2–10.

## Three findings

1. Relevant-but-different-domain is real (crash bandicoot, mashle, semenyo) → gold-overlap
   understates SS quality.
2. Genuine tail noise is also real (lewis cope archive.org dump; tiramisu/bed-bug homonyms)
   → a real re-ranker/tail-precision failure.
3. Intent/coverage mismatch (gallery london, tiramisu recipes, stoke club-vs-city) → neither
   hypothesis; mwmbl's index skews encyclopedic and lacks navigational/transactional pages.
   Standard search shares this gap, so it's not an SS bug.

## Implication

Source selection is not the lever (consistent with prior findings). The bottleneck is the
re-ranker, and its training labels are the root cause: in `mwmbl/rankeval/ltr/dataset.py`,
the label is "URL's rank in Google, else None" — so the ~⅔ relevant-but-off-gold results are
all labelled negative, identical to the homonym junk. The re-ranker literally cannot learn to
tell them apart. → motivates LLM-judged graded relevance labels over the union of results.
