"""Zero-shot nudity check: SigLIP2's image tower scored against fixed text prompts, on a thumbnail held only in memory.

Dedicated NSFW classifiers are trained on porn and hentai against everyday photos, so a statue or a painting is
outside what they learned; NudeNet catches 29% of hand-verified art nudes and Falconsai 2%. A vision-language model
given prompts that name the art forms ("a statue of a nude figure") does not have that blind spot: 96% of the same
nudes, and 94% of nude photographs at the same threshold (docs/image-search-safety-benchmark.md).

The image's embedding is compared with the embedding of each prompt. The softmax over all the prompts, nude and safe
alike, gives the probability mass on the nude ones; at or above the threshold the image is NSFW. The prompts, their
embeddings and the model's temperature are computed once by scripts/export_image_safety_models.py, so the server needs
onnxruntime and nothing else. The prompts are fixed and were not tuned on the images the threshold was fitted on.

The verdict is only ever NSFW or SAFE for a thumbnail that decodes; one that does not is UNCERTAIN, never SAFE.
It is one layer of several and does not detect CSAM.
"""

import io
import json
from pathlib import Path

import numpy as np
import onnxruntime
from django.conf import settings
from PIL import Image

from mwmbl.imagesearch.pixel_classifier import THUMBNAIL_DECODE_ERRORS
from mwmbl.imagesearch.session_options import cpu_options
from mwmbl.imagesearch.types import Verdict
from mwmbl.imagesearch.verdict_cache import digest_files

INPUT_SIZE = 224
# SigLIP2 normalises to [-1, 1].
PIXEL_MEAN = 0.5
PIXEL_STD = 0.5


class ZeroShotSafetyClassifier:
    def __init__(self, model_dir: Path):
        zero_shot_dir = model_dir / "zero_shot"
        self.model_digest = digest_files(
            zero_shot_dir / "model.onnx",
            zero_shot_dir / "text_embeddings.npy",
            zero_shot_dir / "prompts.json",
            zero_shot_dir / "logit.json",
        )
        prompts = json.loads((zero_shot_dir / "prompts.json").read_text())
        self.nude_prompt_count = len(prompts["nude"])
        self.text_embeddings = np.load(zero_shot_dir / "text_embeddings.npy")
        if len(self.text_embeddings) != self.nude_prompt_count + len(prompts["safe"]):
            raise ValueError("zero_shot/text_embeddings.npy does not have one row per prompt in prompts.json")
        logit = json.loads((zero_shot_dir / "logit.json").read_text())
        self.logit_scale, self.logit_bias = logit["scale"], logit["bias"]
        self.session = onnxruntime.InferenceSession(
            str(zero_shot_dir / "model.onnx"),
            cpu_options(settings.IMAGE_SEARCH_ZERO_SHOT_THREADS),
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name

    @property
    def fingerprint(self) -> str:
        """Everything that decides a thumbnail's verdict, so a cached verdict is used only while it still holds."""
        return ":".join(
            str(part) for part in (self.model_digest, settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD, INPUT_SIZE)
        )

    def preprocess(self, thumbnail: Image.Image) -> np.ndarray:
        resized = thumbnail.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
        array = (np.asarray(resized, dtype=np.float32) / 255.0 - PIXEL_MEAN) / PIXEL_STD
        return array.transpose(2, 0, 1)[np.newaxis]

    def nude_probability(self, thumbnail: Image.Image) -> float:
        (features,) = self.session.run(["features"], {self.input_name: self.preprocess(thumbnail)})
        unit_features = features[0] / np.linalg.norm(features[0])
        logits = self.logit_scale * (self.text_embeddings @ unit_features) + self.logit_bias
        shifted = logits - logits.max()
        probabilities = np.exp(shifted) / np.exp(shifted).sum()
        return float(probabilities[: self.nude_prompt_count].sum())

    def classify_bytes(self, data: bytes) -> Verdict:
        try:
            probability = self.nude_probability(Image.open(io.BytesIO(data)))
        except THUMBNAIL_DECODE_ERRORS:
            return Verdict.UNCERTAIN
        return Verdict.NSFW if probability >= settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD else Verdict.SAFE
