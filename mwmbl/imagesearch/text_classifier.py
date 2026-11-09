"""Text safety classifier: a DistilBERT NSFW model, exported to ONNX, run in-process.

Only a text the model confidently calls safe is SAFE. The split between NSFW and UNCERTAIN
follows the two thresholds in settings; both are excluded from results. The model reads 128
tokens at a time, so a longer text is split into chunks and judged by its worst chunk, and a
text too long to check at all is UNCERTAIN. The model is English-only.
"""

import json
from pathlib import Path

import numpy as np
import onnxruntime
from django.conf import settings
from tokenizers import Encoding, Tokenizer

from mwmbl.imagesearch.session_options import cpu_options
from mwmbl.imagesearch.types import Verdict
from mwmbl.imagesearch.verdict_cache import digest_files

MAX_TOKENS = 128
# Smaller batches ran faster than larger ones when measured: attention cost grows with the square
# of the length, and a bigger batch has more length spread to pad.
BATCH_SIZE = 8
# Words, not tokens, so a text can be split without tokenizing it twice. Sixty words fit in 128
# tokens for ordinary text; a chunk that does not is caught as overflowing and made UNCERTAIN.
WORDS_PER_CHUNK = 60
MAX_CHUNKS = 8
NSFW_LABEL = "nsfw"
SEVERITY = [Verdict.SAFE, Verdict.UNCERTAIN, Verdict.NSFW]


class TextSafetyClassifier:
    def __init__(self, model_dir: Path):
        text_dir = model_dir / "text"
        self.model_digest = digest_files(text_dir / "model.onnx", text_dir / "tokenizer.json", text_dir / "labels.json")
        self.tokenizer = Tokenizer.from_file(str(text_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding(length=None)

        labels = json.loads((text_dir / "labels.json").read_text())
        self.nsfw_index = next(int(index) for index, label in labels.items() if label.lower() == NSFW_LABEL)

        self.session = onnxruntime.InferenceSession(
            str(text_dir / "model.onnx"),
            cpu_options(settings.IMAGE_SEARCH_TEXT_THREADS),
            providers=["CPUExecutionProvider"],
        )

    @property
    def fingerprint(self) -> str:
        """Everything that decides a text's verdict, so a cached verdict is used only while it still holds."""
        return ":".join(
            str(part)
            for part in (
                self.model_digest,
                settings.IMAGE_SEARCH_TEXT_NSFW_THRESHOLD,
                settings.IMAGE_SEARCH_TEXT_UNCERTAIN_THRESHOLD,
                MAX_TOKENS,
                WORDS_PER_CHUNK,
                MAX_CHUNKS,
            )
        )

    def nsfw_probabilities(self, encodings: list[Encoding]) -> np.ndarray:
        """Each chunk's NSFW probability, run in small batches of similar length.

        The tokenizer pads every chunk to the longest in the call, but most chunks are far shorter than
        that, and the model's cost grows with the padded length. Sorting by length and trimming each batch
        to its longest chunk drops the padding without changing a chunk's logits, since padding is masked.
        """
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        lengths = attention_mask.sum(axis=1)
        order = np.argsort(lengths, kind="stable")

        probabilities = np.empty(len(encodings))
        for start in range(0, len(order), BATCH_SIZE):
            rows = order[start : start + BATCH_SIZE]
            width = lengths[rows].max()
            inputs = {"input_ids": input_ids[rows, :width], "attention_mask": attention_mask[rows, :width]}
            (logits,) = self.session.run(["logits"], inputs)
            shifted = logits - logits.max(axis=1, keepdims=True)
            batch_probabilities = np.exp(shifted) / np.exp(shifted).sum(axis=1, keepdims=True)
            probabilities[rows] = batch_probabilities[:, self.nsfw_index]
        return probabilities

    def chunk_verdicts(self, chunks: list[str]) -> list[Verdict]:
        encodings = self.tokenizer.encode_batch(chunks)
        nsfw_probabilities = self.nsfw_probabilities(encodings)
        return [
            # A chunk the model could not read in full is not one it can vouch for.
            Verdict.UNCERTAIN if encoding.overflowing else verdict_for_probability(float(probability))
            for encoding, probability in zip(encodings, nsfw_probabilities)
        ]

    def classify(self, texts: list[str]) -> list[Verdict]:
        """One verdict per text: the worst verdict of any chunk of it, so no part escapes the model."""
        split = [split_into_chunks(text) for text in texts]
        all_chunks = [chunk for chunks in split if chunks is not None for chunk in chunks]
        verdicts = iter(self.chunk_verdicts(all_chunks)) if all_chunks else iter([])

        results = []
        for chunks in split:
            if chunks is None:
                results.append(Verdict.UNCERTAIN)
            else:
                results.append(max((next(verdicts) for _ in chunks), key=SEVERITY.index))
        return results


def split_into_chunks(text: str) -> list[str] | None:
    """The text in pieces the model reads whole, or None if there is too much of it to check."""
    words = text.split()
    if not words or len(words) > WORDS_PER_CHUNK * MAX_CHUNKS:
        return None
    return [" ".join(words[start : start + WORDS_PER_CHUNK]) for start in range(0, len(words), WORDS_PER_CHUNK)]


def verdict_for_probability(nsfw_probability: float) -> Verdict:
    if nsfw_probability >= settings.IMAGE_SEARCH_TEXT_NSFW_THRESHOLD:
        return Verdict.NSFW
    if nsfw_probability >= settings.IMAGE_SEARCH_TEXT_UNCERTAIN_THRESHOLD:
        return Verdict.UNCERTAIN
    return Verdict.SAFE
