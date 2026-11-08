"""Text safety classifier: a DistilBERT NSFW model, exported to ONNX, run in-process.

Only a text the model confidently calls safe is SAFE. The split between NSFW and UNCERTAIN
follows the two thresholds in settings; both are excluded from results.
"""

import json
from pathlib import Path

import numpy as np
import onnxruntime
from django.conf import settings
from tokenizers import Tokenizer

from mwmbl.imagesearch.types import Verdict

MAX_TOKENS = 128
NSFW_LABEL = "nsfw"


class TextSafetyClassifier:
    def __init__(self, model_dir: Path):
        text_dir = model_dir / "text"
        self.tokenizer = Tokenizer.from_file(str(text_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding(length=None)

        labels = json.loads((text_dir / "labels.json").read_text())
        self.nsfw_index = next(int(index) for index, label in labels.items() if label.lower() == NSFW_LABEL)

        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(
            str(text_dir / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )

    def nsfw_probabilities(self, texts: list[str]) -> np.ndarray:
        encodings = self.tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        (logits,) = self.session.run(["logits"], {"input_ids": input_ids, "attention_mask": attention_mask})
        shifted = logits - logits.max(axis=1, keepdims=True)
        probabilities = np.exp(shifted) / np.exp(shifted).sum(axis=1, keepdims=True)
        return probabilities[:, self.nsfw_index]

    def classify(self, texts: list[str]) -> list[Verdict]:
        if not texts:
            return []
        return [verdict_for_probability(float(p)) for p in self.nsfw_probabilities(texts)]


def verdict_for_probability(nsfw_probability: float) -> Verdict:
    if nsfw_probability >= settings.IMAGE_SEARCH_TEXT_NSFW_THRESHOLD:
        return Verdict.NSFW
    if nsfw_probability >= settings.IMAGE_SEARCH_TEXT_UNCERTAIN_THRESHOLD:
        return Verdict.UNCERTAIN
    return Verdict.SAFE
