# Image search safety: model benchmark and recommendations

Policy: **no nudity at all, art and medical included.** A missed nude is the costly error; excluding a safe image is acceptable.

## TL;DR

- The pixel layer is the weak link. NudeNet catches **29% of art nudes** and **73% of nude photographs**. Dedicated NSFW classifiers (Falconsai, AdamCodd, Marqo, LukeJacob, Freepik, Blue-Eye…) are no better: they are trained on porn/hentai vs. everyday photos, so statues and paintings are out-of-distribution (Falconsai: 1 of 52; Blue-Eye, by its own definition of "safe", 10 of 52).
- **Zero-shot SigLIP2 with art-aware prompts is the only candidate that fixes it:** 94–96% recall on art *and* photographs, ~6–17% of safe images excluded depending on the threshold. It costs ~11× NudeNet's CPU (340 ms vs 30 ms per image).
- **The per-image DistilBERT text check adds nothing measurable** (0 of 22 rule-evading nudes caught) yet is the single biggest CPU cost and excludes 4–8% of safe images. The deny-list rules do the real work (58% of art nudes, 91% of nude photographs).
- Zero false negatives is not achievable by any automated filter. Keep fail-closed behaviour and a report/takedown path.

## Recommendations

| # | Action | Where | Effort | Effect |
|---|---|---|---|---|
| 1 | **Drop per-image DistilBERT** (done).  Keep the deny rules and the cheap query gate (`query_is_safe`). | [pipeline.py](../mwmbl/imagesearch/pipeline.py) `pass_text_checks`, `text_verdicts` | S | Same recall; false positives on safe images 8% → 4%; ~5 s less CPU per cold search; drops a 268 MB model |
| 2 | **Add SigLIP2 zero-shot as a second pixel layer** (flag if NudeNet *or* SigLIP2 flags). Export the vision tower to fp32 ONNX with the 24 prompt embeddings precomputed; start at threshold **0.39**; include prompts + threshold in the verdict-cache fingerprint. Do **not** quantize (int8: recall 94% → 75%). | [pixel_classifier.py](../mwmbl/imagesearch/pixel_classifier.py), [export_image_safety_models.py](../scripts/export_image_safety_models.py), [verdict_cache.py](../mwmbl/imagesearch/verdict_cache.py) | M | Art recall 29% → ~98% and photo recall 73% → ~96% (models alone, before rules); ~+4 s CPU per cold search after #1 |
| 3 | **Ship in shadow mode first:** run SigLIP2 beside the live pipeline, log scores and would-be decisions only, and tune the threshold on real traffic. No one has to view explicit images. | pipeline + logging | S | Real false-positive volume before it affects users |
| 4 | **Move the cost off the request path.** Run SigLIP2 in batch over Commons results and serve from an index of approved images (Daoud's idea). Until then: only score images that survive the rules, verify lazily in rank order until `limit` are approved, keep the 30-day verdict cache. | pipeline / new batch job | M–L | Removes the per-search CPU problem rather than shrinking it |
| 5 | **Distil a fast student** from SigLIP2 labels over many Commons images (a better teacher than NudeNet for Blue's plan). Target 15–30 ms per image. Needs GPU or ~94 CPU-hours per million images. | new | L | Cheap *and* accurate |
| 6 | **Close the validation gaps** (below): an independent art set, a human-reviewed photographic set, athlete/dancer prompts to cut false positives. | benchmark | M | Confidence in the thresholds |
| 7 | **Do not adopt** Falconsai, AdamCodd, Marqo, LukeJacob, Freepik (2.2 s/image), CLIP-L/14, NudeNet 640m (31× slower, worse). **Blue-Eye is now measured and rejected:** very precise (0% of safe images excluded at its default) but its "safe" class includes non-sexual art and medical images, so it labels 42 of 52 art nudes safe, and it costs 7.5 s/image. | - | - | - |

Budget fallback for #2: CLIP-B/32 costs 92 ms/image (2.6 s per cold search, cheaper than today) but excludes ~20% of safe images.

## Results

All 16 image models were scored on the same images (Blue-Eye on a 550-image subset, see †); thumbnails (320 px, as production sees them) were fetched in memory and never stored.
**Art recall** is on 52 hand-verified art nudes; **photo recall** is on 331 Commons photographs labelled by category (unreviewed). Both at each model's default threshold (NudeNet: production thresholds).
**FP@95%** is the share of verified-safe images excluded when the threshold is set to keep 95% of art nudes (cross-validated). **Cold search** = 28 images, ignoring the verdict cache.

| Model | Art recall | Photo recall | FP@95% | ms/img | Cold search CPU |
|---|---|---|---|---|---|
| **SigLIP2-B/16 zero-shot** | 96% | 94% | **6.7%** | 340 | 9.5 s |
| lovetillion (FocalNet) | 67% | 84% | 10.7% | 312 | 8.7 s |
| CLIP-B/32 zero-shot | 92% | 88% | 19.7% | 92 | 2.6 s |
| Ateeqq (SigLIP2 fine-tune) | 38% | 64% | 22.6% | 330 | 9.2 s |
| CLIP-L/14 zero-shot | 96% | 95% | 25.4% | 1558 | 43.6 s |
| Freepik EVA02 | 44% | 78% | 40.7% | 2182 | 61 s |
| **NudeNet 320n (current)** | 29% | 73% | 48.3% | 30 | 0.8 s |
| Blue-Eye (DINOv3 ViT-L, 512 px) † | 19% | 74% | 49.7%† | 7,523 | 211 s |
| viddexa-2-mini / nano | 33% / 33% | 67% / 64% | 55% / 55% | 133 / 15 | 3.7 / 0.4 s |
| NudeNet 640m | 23% | 74% | 57.2% | 919 | 25.7 s |
| Marqo-384 | 8% | 51% | 68.0% | 100 | 2.8 s |
| AdamCodd | 10% | 66% | 77.5% | 1071 | 30 s |
| taufiqdp MobileNetV4 | 17% | 52% | 81.9% | 6 | 0.2 s |
| Falconsai | 2% | 43% | 82.8% | 327 | 9.1 s |
| LukeJacob | 4% | 63% | 89.4% | 341 | 9.5 s |
| *DistilBERT text (today)* | 12% (0/22 rule-evading) | not scored | n/a | ~190 | ~5.3 s |

† Blue-Eye was scored on a representative 550-image subset (all 52 art nudes and 88 clean images, 150 photographs, 150 safe, 30 medical, 20 clothed cleavage), so its FP@95% is not directly comparable: SigLIP2 on the same subset is 8.8% (6.7% on the full set). Its photo recall counts "suggestive" + "explicit" ("explicit" alone: 61%).

Runtime is ONNX Runtime, one thread, idle spinning off (the production setting), whole decode → preprocess → inference path, median of 25. All licences are permissive (Apache-2.0 or MIT).

**Whole pipeline (rules + models).** Thresholds fitted on the art set, then applied unchanged to the photographs, so the photo column is out-of-sample.

| Pipeline | Art recall (52) | Photo recall (331) | Safe images excluded |
|---|---|---|---|
| Today: rules + DistilBERT + NudeNet | 71% | n/a | 8% |
| Drop DistilBERT | 71% | n/a | 4% |
| Rules + NudeNet + SigLIP2 @0.39 | ~100% | 96% (models alone) | ~17–20% |
| Rules + NudeNet + SigLIP2 @0.68 | 98% | 94% (models alone) | ~7–9% |

## What the data says

- **Rules first.** The deny list already catches 58% of art nudes and 91% of nude photographs by their Commons text at zero CPU. The models matter for what the rules miss: only 22 of the 52 art nudes and 29 of the 331 photographs get past the rules.
- **SigLIP2's strength is its art-specific prompts.** With only photo-style prompts (no art-specific ones) its AUC is 0.71 (66% false positives at 95% recall). Its false positives are mostly athletes and dancers (ballet, football, tennis).
- **Blue-Eye is a precise explicit-photo detector, not an art-nudity filter.** At its default it excludes 0 of 238 verified-safe images and catches 74% of nude photographs, but 42 of 52 art nudes get "safe". Raising its threshold to catch them excludes ~50–59% of safe images, and it costs 22× SigLIP2's CPU.
- **NudeNet also hallucinates:** 4 of the 26 images it was "confident" about were birds, a solar transit and a rope knot. SigLIP2 agreed with all 22 plausible detections.
- **Quantization is not free.** int8 changed DistilBERT's verdicts and cut SigLIP2's recall from 94% to 75%.

## Limits

- Art ground truth is 52 images I labelled by looking (art categories only). Category names alone were too noisy: about two thirds of the "misses" in one audit were not nude.
- Photographs are labelled by Commons category and **not reviewed**; "rule-evading" photographs (n=29) are the noisiest, so treat that column as indicative only.
- With 52 positives, 0 misses still means a ≤7% true miss rate at 95% confidence. Thresholds on the art set were fitted in-sample for the "Rules + …" art numbers.
- The prompts were written once, before any results, and not tuned. Commons only; English only; no CSAM detection (nothing here claims to).
- Not tested: EraX YOLO (AGPL runtime), non-commercial licences, and ~25 clones/conversions of tested lineages. Surveyed: ~95 Hub models.
- The benchmark harness and per-image scores are not in the repo. A follow-up PR can add them if wanted.
