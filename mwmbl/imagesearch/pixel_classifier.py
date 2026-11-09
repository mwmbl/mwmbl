"""Pixel safety check: NudeNet's body-part detector, run on a thumbnail held only in memory.

The thumbnail is fetched from Wikimedia's image hosts, decoded, scored and dropped; it is never
written anywhere. A thumbnail that cannot be fetched or decoded is UNCERTAIN, never SAFE.

NudeNet is a detector, not a classifier of sexual content: it finds body parts. It catches
exposed anatomy; it cannot tell that an innocent-looking photo is something else, and it
does not detect CSAM. It is one layer of several.
"""

import io
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np
import onnxruntime
from django.conf import settings
from PIL import Image, UnidentifiedImageError

from mwmbl.imagesearch.commons import ALLOWED_THUMBNAIL_HOSTS, SourceRejectedRequest, get_with_backoff
from mwmbl.imagesearch.session_options import cpu_options
from mwmbl.imagesearch.types import Verdict

INPUT_SIZE = 320
BOX_VALUES = 4
CLASS_NAMES = [
    "FEMALE_GENITALIA_COVERED",
    "FACE_FEMALE",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_COVERED",
    "FEET_COVERED",
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "FACE_MALE",
    "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
]
EXPOSED_CLASSES = {
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
}
COVERED_CLASSES = {
    "FEMALE_GENITALIA_COVERED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
}
EXPOSED_INDEXES = [i for i, name in enumerate(CLASS_NAMES) if name in EXPOSED_CLASSES]
COVERED_INDEXES = [i for i, name in enumerate(CLASS_NAMES) if name in COVERED_CLASSES]

# Decoding a hostile image can allocate a lot; a 320px thumbnail never needs this much.
Image.MAX_IMAGE_PIXELS = 4_000_000


class PixelSafetyClassifier:
    def __init__(self, model_dir: Path):
        self.session = onnxruntime.InferenceSession(
            str(model_dir / "nudenet" / "320n.onnx"),
            cpu_options(settings.IMAGE_SEARCH_PIXEL_THREADS),
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name

    def preprocess(self, thumbnail: Image.Image) -> np.ndarray:
        rgb = thumbnail.convert("RGB")
        side = max(rgb.size)
        square = Image.new("RGB", (side, side))
        square.paste(rgb, (0, 0))
        resized = square.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        return array.transpose(2, 0, 1)[np.newaxis]

    def best_scores(self, thumbnail: Image.Image) -> np.ndarray:
        """Highest detection score anywhere in the image for each class."""
        (output,) = self.session.run(None, {self.input_name: self.preprocess(thumbnail)})
        class_scores = output[0][BOX_VALUES:]
        return class_scores.max(axis=1)

    def classify_bytes(self, data: bytes) -> Verdict:
        try:
            thumbnail = Image.open(io.BytesIO(data))
            scores = self.best_scores(thumbnail)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return Verdict.UNCERTAIN
        return verdict_for_scores(scores)


def verdict_for_scores(scores: np.ndarray) -> Verdict:
    if scores[EXPOSED_INDEXES].max() >= settings.IMAGE_SEARCH_PIXEL_EXPOSED_THRESHOLD:
        return Verdict.NSFW
    if scores[COVERED_INDEXES].max() >= settings.IMAGE_SEARCH_PIXEL_COVERED_THRESHOLD:
        return Verdict.UNCERTAIN
    return Verdict.SAFE


def fetch_thumbnail(client: httpx.Client, url: str) -> bytes | None:
    """The thumbnail's bytes, or None if this thumbnail cannot be used.

    A refusal of this one thumbnail excludes just its image. Wikimedia being unreachable or asking
    us to back off raises ImageSourceUnavailable instead: it says nothing about the image, so it must
    not be turned into a result that looks final and gets cached.
    """
    if urlparse(url).hostname not in ALLOWED_THUMBNAIL_HOSTS:
        return None
    try:
        response = get_with_backoff(client, url)
    except SourceRejectedRequest:
        return None
    if len(response.content) > settings.IMAGE_SEARCH_THUMBNAIL_MAX_BYTES:
        return None
    return response.content
