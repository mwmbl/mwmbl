"""Image search's own safety layers on every collected candidate: the deny rules, NudeNet and SigLIP2.

Each distinct file is judged once. Its thumbnail is fetched into memory, scored and dropped, as in the pipeline;
it is never written to disk. Scores are kept with the verdicts, for trying other thresholds. A run resumes.
"""

import io

import httpx
import numpy as np
from django.conf import settings
from PIL import Image

from mwmbl.imagesearch import pixel_classifier, rules
from mwmbl.imagesearch.commons import new_client
from mwmbl.imagesearch.pipeline import file_id
from mwmbl.imagesearch.pixel_classifier import THUMBNAIL_DECODE_ERRORS, PixelSafetyClassifier, fetch_thumbnail
from mwmbl.imagesearch.types import SourceImage, Verdict
from mwmbl.imagesearch.zero_shot_classifier import ZeroShotSafetyClassifier
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR, run_resumably
from mwmbl.rankeval.seed_images.collect import load_candidates, with_backoff
from mwmbl.rankeval.seed_images.queries import load_queries

VERDICTS_PATH = SEED_IMAGES_DIR / "local_verdicts.jsonl"


def unique_candidates() -> dict[str, SourceImage]:
    return {file_id(image): image for query in load_queries() for image in load_candidates(query)}


def pixel_scores(thumbnail: Image.Image, nudenet: PixelSafetyClassifier, zero_shot: ZeroShotSafetyClassifier) -> dict:
    nudenet_scores = nudenet.best_scores(thumbnail)
    probability = zero_shot.nude_probability(thumbnail)
    zero_shot_verdict = Verdict.NSFW if probability >= settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD else Verdict.SAFE
    return {
        "nudenet": pixel_classifier.verdict_for_scores(nudenet_scores).value,
        "nudenet_exposed": float(np.max(nudenet_scores[pixel_classifier.EXPOSED_INDEXES])),
        "nudenet_covered": float(np.max(nudenet_scores[pixel_classifier.COVERED_INDEXES])),
        "zero_shot": zero_shot_verdict.value,
        "zero_shot_probability": probability,
    }


def judge(client: httpx.Client, image: SourceImage, nudenet, zero_shot) -> dict:
    data, fetch_latency = with_backoff(lambda: fetch_thumbnail(client, image.thumbnail_url))
    record = {
        "file_id": file_id(image),
        "page_url": image.page_url,
        "rules": rules.image_verdict(image).value,
        "denied_term": rules.find_denied_term(image.text),
        "thumbnail_fetched": data is not None,
        "fetch_latency_s": fetch_latency,
    }
    # As in the pipeline, a thumbnail that cannot be fetched or decoded is UNCERTAIN, never SAFE.
    uncertain = {"nudenet": Verdict.UNCERTAIN.value, "zero_shot": Verdict.UNCERTAIN.value}
    if data is None:
        return record | uncertain
    try:
        return record | pixel_scores(Image.open(io.BytesIO(data)), nudenet, zero_shot)
    except THUMBNAIL_DECODE_ERRORS:
        return record | uncertain


def main() -> None:
    nudenet = PixelSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
    zero_shot = ZeroShotSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
    candidates = unique_candidates()
    with new_client() as client:
        run_resumably(
            VERDICTS_PATH, "file_id", list(candidates), lambda file: judge(client, candidates[file], nudenet, zero_shot)
        )


if __name__ == "__main__":
    main()
