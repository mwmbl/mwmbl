"""Inputs shared by the query-gate and image-ranking evaluation: the Jev-safe candidates and the gate directory."""

from mwmbl.imagesearch.pipeline import file_id
from mwmbl.imagesearch.prompt import NSFW_THRESHOLD
from mwmbl.imagesearch.types import SourceImage
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR
from mwmbl.rankeval.seed_images.collect import load_candidates
from mwmbl.rankeval.seed_images.queries import read_jsonl

GATE_DIR = SEED_IMAGES_DIR / "gate"
NSFW_PATH = SEED_IMAGES_DIR / "jev_nsfw.jsonl"
RELEVANCE_PATH = GATE_DIR / "jev_relevance.jsonl"


def below_threshold(images: list[SourceImage], nsfw_by_file: dict[str, float]) -> list[SourceImage]:
    return [image for image in images if nsfw_by_file[file_id(image)] < NSFW_THRESHOLD]


def jev_safe_candidates() -> dict[str, list[SourceImage]]:
    """Each query with candidates, mapped to those Jev's NSFW score leaves in, in Commons order."""
    safe = {}
    for row in read_jsonl(NSFW_PATH):
        nsfw_by_file = dict(zip(row["file_ids"], row["scores"]))
        safe[row["query"]] = below_threshold(load_candidates(row["query"]), nsfw_by_file)
    return safe
