"""The arms of the holistic comparison of Commons images in Seed Search, on the en-gb queries, and their batches.

- A0: production's order, engb_baseline_lists.json.
- A2, the recommended design: when Jev's image need clears the gate in gate/recommended.json, a strip of up to k
  images is one extra entry at position 2, after the top web result, displacing none. The images are the Jev-safe
  candidates at or above the minimum Jev image relevance, highest first. Otherwise the list is A0's.
- A1: no gate. The two most relevant Jev-safe images join the web results as single results, merged by Jev
  composite, and the list is cut back to ten. An image's composite treats its Jev image relevance as a web
  result's relevance, from a substantive page (quality 2) that Staan didn't return, so it is relevance - 0.5.

    composite SCORES ROWS   the web results' Jev composite, from mwmbl#462's jev_composite_scores.json and
                            Staan's lists in rows-0.05.json -> holistic/engb_web_composite.json
    arms                    holistic/a1.json, holistic/a2.json and judge batches for each against A0
    vision                  judge batches for about 20 A2-vs-A0 comparisons, with the strips' thumbnails, from
                            images every local safety layer passed. The thumbnails are written to holistic/thumbs/:
                            a user-approved exception to keeping them in memory only, to delete after judging.
    splits                  once judged and consolidated: A2 vs A0 by whether the strip passed every local
                            safety layer, and the vision check against the text-only verdicts
"""

import json
import random
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

from mwmbl.imagesearch.commons import new_client
from mwmbl.imagesearch.pipeline import file_id
from mwmbl.imagesearch.pixel_classifier import fetch_thumbnail
from mwmbl.imagesearch.types import SourceImage
from mwmbl.rankeval.evaluation import holistic_judge
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR
from mwmbl.rankeval.seed_images.collect import with_backoff
from mwmbl.rankeval.seed_images.gate_analysis import RECOMMENDED_PATH
from mwmbl.rankeval.seed_images.gate_data import RELEVANCE_PATH, jev_safe_candidates
from mwmbl.rankeval.seed_images.label_batches import passed_every_layer, save_thumbnail
from mwmbl.rankeval.seed_images.local_verdicts import VERDICTS_PATH
from mwmbl.rankeval.seed_images.queries import ENGB_BASELINE_PATH, NUM_RESULTS, read_jsonl
from mwmbl.rankeval.seed_images.run_jev import NEED_PATH
from mwmbl.tinysearchengine.jev_rank import (
    INDEX_PENALTY,
    NOT_IN_STAAN,
    QUALITY_WEIGHT,
    RELEVANCE_WEIGHT,
    STAAN_POSITION_WEIGHT,
)

HOLISTIC_DIR = SEED_IMAGES_DIR / "holistic"
WEB_COMPOSITE_PATH = HOLISTIC_DIR / "engb_web_composite.json"
A1_PATH = HOLISTIC_DIR / "a1.json"
A2_PATH = HOLISTIC_DIR / "a2.json"
A2_VISION_PATH = HOLISTIC_DIR / "a2_vision.json"
THUMBS_DIR = HOLISTIC_DIR / "thumbs"
A1_MAX_IMAGES = 2
SUBSTANTIVE = 2
VISION_COMPARISONS = 20
SEED = 610


def composite(relevance: float, quality: float, staan_position: int) -> float:
    """Jev composite + Staan rank's score, as jev_rank.composite_order computes it."""
    from_index = staan_position == NOT_IN_STAAN
    return (
        RELEVANCE_WEIGHT * relevance
        + QUALITY_WEIGHT * quality
        - STAAN_POSITION_WEIGHT * staan_position
        - INDEX_PENALTY * from_index
    )


def image_composite(relevance: float) -> float:
    return composite(relevance, SUBSTANTIVE, NOT_IN_STAAN)


def web_composite(scores_path: Path, rows_path: Path) -> None:
    scores = json.loads(scores_path.read_text())
    staan = {row["query"]: row["lists"]["staan"][:NUM_RESULTS] for row in json.loads(rows_path.read_text())}
    baseline = json.loads(ENGB_BASELINE_PATH.read_text())
    composites = {}
    for query, results in baseline.items():
        positions = [staan[query].index(r["url"]) if r["url"] in staan[query] else NOT_IN_STAAN for r in results]
        # Jev's columns there are relevance, entity and quality; production's composite leaves entity out.
        jev = [scores[f"{query}\t{result['url']}"] for result in results]
        composites[query] = [
            composite(relevance, quality, position) for (relevance, _, quality), position in zip(jev, positions)
        ]
        assert composites[query] == sorted(composites[query], reverse=True), f"{query}: not in composite order"
    HOLISTIC_DIR.mkdir(exist_ok=True)
    WEB_COMPOSITE_PATH.write_text(json.dumps(composites))
    print(f"{len(composites)} queries -> {WEB_COMPOSITE_PATH}")


def ranked_images(images: list[SourceImage], relevance: dict[str, float]) -> list[tuple[SourceImage, float]]:
    """Each image with its Jev relevance, highest first, ties in Commons' order."""
    scored = [(image, relevance[file_id(image)]) for image in images]
    return sorted(scored, key=lambda pair: (-pair[1], pair[0].rank))


def strip_entry(images: list[SourceImage]) -> dict:
    shown = [{"title": image.title, "description": image.description, "url": image.page_url} for image in images]
    return {"url": "commons-strip:" + " ".join(image.page_url for image in images), "images": shown}


def image_entry(image: SourceImage) -> dict:
    return {"url": image.page_url, "title": image.title, "extract": image.description, "image": True}


def with_strip(web: list[dict], scored: list[tuple[SourceImage, float]], gate: dict) -> list[dict]:
    shown = [image for image, relevance in scored if relevance >= gate["min_jev_relevance"]][: gate["num_images"]]
    if not shown:
        return web
    return web[:1] + [strip_entry(shown)] + web[1:]


def merged(web: list[dict], composites: list[float], scored: list[tuple[SourceImage, float]]) -> list[dict]:
    """The web results and the most relevant images by composite, web first on a tie, cut back to ten."""
    entries = [(score, 0, position, result) for position, (result, score) in enumerate(zip(web, composites))]
    for position, (image, relevance) in enumerate(scored[:A1_MAX_IMAGES]):
        entries.append((image_composite(relevance), 1, position, image_entry(image)))
    entries.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    return [entry[3] for entry in entries[:NUM_RESULTS]]


def scored_candidates(queries: list[str]) -> dict[str, list[tuple[SourceImage, float]]]:
    relevance = {row["query"]: dict(zip(row["file_ids"], row["scores"])) for row in read_jsonl(RELEVANCE_PATH)}
    safe = jev_safe_candidates()
    return {query: ranked_images(safe[query], relevance[query]) for query in queries if safe.get(query)}


def build_arms() -> None:
    gate = json.loads(RECOMMENDED_PATH.read_text())
    baseline = json.loads(ENGB_BASELINE_PATH.read_text())
    composites = json.loads(WEB_COMPOSITE_PATH.read_text())
    need = {row["query"]: row["score"] for row in read_jsonl(NEED_PATH)}
    scored = scored_candidates(list(baseline))
    a1, a2 = {}, {}
    for query, web in baseline.items():
        images = scored.get(query, [])
        gated_in = need[query] >= gate["jev_need_threshold"]
        a2[query] = with_strip(web, images, gate) if gated_in else web
        a1[query] = merged(web, composites[query], images)
    A1_PATH.write_text(json.dumps(a1))
    A2_PATH.write_text(json.dumps(a2))
    gated = sum(need[query] >= gate["jev_need_threshold"] for query in baseline)
    strips = [entry for results in a2.values() for entry in results if "images" in entry]
    inserted = [entry for results in a1.values() for entry in results if "image" in entry]
    print(f"A2: {gated} of {len(baseline)} gated in, {len(strips)} with a strip, ", end="")
    print(f"{sum(len(strip['images']) for strip in strips)} images")
    print(f"A1: {len(inserted)} images in {sum(any('image' in e for e in r) for r in a1.values())} queries' top tens")
    holistic_judge.batches(A2_PATH, ENGB_BASELINE_PATH, HOLISTIC_DIR / "a2_vs_a0")
    holistic_judge.batches(A1_PATH, ENGB_BASELINE_PATH, HOLISTIC_DIR / "a1_vs_a0")


def strips_and_passed(a2: dict) -> tuple[dict[str, dict], list[str]]:
    """Each A2 strip by query, and the queries whose strip's every image passed every local safety layer."""
    verdicts = {row["page_url"]: row for row in read_jsonl(VERDICTS_PATH)}
    strips = {
        query: next(entry for entry in results if "images" in entry)
        for query, results in a2.items()
        if any("images" in entry for entry in results)
    }
    passed = sorted(
        query
        for query, strip in strips.items()
        if all(passed_every_layer(verdicts[image["url"]]) for image in strip["images"])
    )
    return strips, passed


def print_summary(name: str, records: list[dict]) -> None:
    summary = holistic_judge.summarise(records)
    low, high = summary["ci"]
    print(
        f"| {name} | {summary['comparisons']} | {summary['preference']:+.2f} [{low:+.2f}, {high:+.2f}] | "
        f"{summary['x_preferred']:.0%} / {summary['y_preferred']:.0%} / {summary['tie']:.0%} | "
        f"{summary['orders_agree']:.0%} |"
    )


def splits() -> None:
    """A2 vs A0 split by whether the strip passed every local layer, and the vision check against text only."""
    _, passed = strips_and_passed(json.loads(A2_PATH.read_text()))
    text = [json.loads(line) for line in open(HOLISTIC_DIR / "a2_vs_a0" / "judgments.jsonl")]
    vision = [json.loads(line) for line in open(HOLISTIC_DIR / "a2_vs_a0_vision" / "judgments.jsonl")]
    vision_queries = {record["query"] for record in vision}
    print("| Comparison | Queries | Preference for A2 [95% CI] | A2 / A0 / tie | Orders agree |\n|---|---|---|---|---|")
    print_summary("Strip passed every local layer", [record for record in text if record["query"] in passed])
    print_summary("Strip flagged by a local layer", [record for record in text if record["query"] not in passed])
    print_summary("Vision subset, text only", [record for record in text if record["query"] in vision_queries])
    print_summary("Vision subset, with thumbnails", vision)

    def preference_by_query(records: list[dict]) -> dict[str, float]:
        by_query: dict[str, list[int]] = {}
        for record in records:
            by_query.setdefault(record["query"], []).append(holistic_judge.preference_for_x(record))
        return {query: sum(values) / len(values) for query, values in by_query.items()}

    text_by_query, vision_by_query = preference_by_query(text), preference_by_query(vision)
    # A tie only agrees with a tie, so a tie against a win for A2 counts as a disagreement.
    same_sign = sum(np.sign(text_by_query[query]) == np.sign(vision_by_query[query]) for query in vision_by_query)
    print(f"\nText-only and vision verdicts agree in direction on {same_sign} of {len(vision_by_query)} queries")


def build_vision() -> None:
    a2 = json.loads(A2_PATH.read_text())
    strips, passed = strips_and_passed(a2)
    print(f"{len(passed)} of {len(strips)} strips passed every local layer")
    THUMBS_DIR.mkdir(exist_ok=True)
    thumbnails = {image.page_url: image.thumbnail_url for images in jev_safe_candidates().values() for image in images}
    vision = {}
    with new_client() as client:
        for query in random.Random(SEED).sample(passed, len(passed)):
            if len(vision) == VISION_COMPARISONS:
                break
            strip = strips[query]
            images = []
            for number, image in enumerate(strip["images"]):
                data, _ = with_backoff(lambda: fetch_thumbnail(client, thumbnails[image["url"]]))
                thumb = data and save_thumbnail(data, THUMBS_DIR / f"{len(vision):02d}_{number}.jpg")
                images.append(image | {"thumb": thumb})
            if not all(image["thumb"] for image in images):
                continue
            vision[query] = [strip | {"images": images} if entry is strip else entry for entry in a2[query]]
    A2_VISION_PATH.write_text(json.dumps(vision))
    holistic_judge.batches(A2_VISION_PATH, ENGB_BASELINE_PATH, HOLISTIC_DIR / "a2_vs_a0_vision")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    composite_parser = commands.add_parser("composite")
    composite_parser.add_argument("scores", type=Path)
    composite_parser.add_argument("rows", type=Path)
    commands.add_parser("arms")
    commands.add_parser("vision")
    commands.add_parser("splits")
    args = parser.parse_args()

    if args.command == "composite":
        web_composite(args.scores, args.rows)
    elif args.command == "arms":
        build_arms()
    elif args.command == "vision":
        build_vision()
    else:
        splits()


if __name__ == "__main__":
    main()
