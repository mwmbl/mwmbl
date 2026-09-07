"""Local relevance judge: fine-tuned cross-encoder served over ONNX on CPU.

Scores (query, title+extract) pairs in [0, 1] with the judge trained by
scripts/modal_judge_train.py (winner: multi-task MiniLM; see
devdata/judge_train/RESULTS.md). Used at the end of a Super Search to
re-rank the final results for the user and to compute per-source bandit
rewards — replacing the circular "survived the LTR top-K" reward.

The model is loaded lazily from ``settings.SUPER_SEARCH_JUDGE_MODEL_DIR``
(a directory holding ``model.onnx`` + ``tokenizer.json``). If the artifact
or onnxruntime is unavailable the judge is disabled — ``get_judge()``
returns None and callers fall back to LTR ranking / survival rewards — so
deploys without the model artifact keep working unchanged.

Queries pass through this module in memory only; nothing is persisted.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

DOC_CHARS = 1000  # matches judge training (scripts/judge_bakeoff.py DOC_CHARS)
MAX_TOKENS = 256  # matches training max_length
BATCH_SIZE = 64


def doc_text(title: str | None, extract: str | None) -> str:
    """Assemble the document text exactly as the judge was trained on."""
    return f"{title or ''}. {extract or ''}".strip()[:DOC_CHARS]


class Judge:
    def __init__(self, model_dir: Path):
        import onnxruntime
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding()
        self.session = onnxruntime.InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}

    def score(self, query: str, doc_texts: list[str]) -> list[float]:
        """Relevance of each doc text to the query, each in [0, 1]."""
        scores: list[float] = []
        for start in range(0, len(doc_texts), BATCH_SIZE):
            batch = doc_texts[start : start + BATCH_SIZE]
            encodings = self.tokenizer.encode_batch([(query, text) for text in batch])
            feed = {
                "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            }
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
            logits = self.session.run(None, feed)[0][:, 0].astype(np.float64)
            scores.extend(float(s) for s in 1 / (1 + np.exp(-logits)))
        return scores


_judge: Judge | None = None
_load_attempted = False
_lock = threading.Lock()


def get_judge() -> Judge | None:
    """Lazily load the shared judge; None (memoized) if unavailable."""
    global _judge, _load_attempted
    if _load_attempted:
        return _judge
    with _lock:
        if _load_attempted:
            return _judge
        model_dir = Path(getattr(settings, "SUPER_SEARCH_JUDGE_MODEL_DIR", ""))
        try:
            if (model_dir / "model.onnx").exists():
                _judge = Judge(model_dir)
                logger.info("relevance judge loaded from %s", model_dir)
            else:
                logger.warning(
                    "relevance judge model not found at %s; falling back to LTR ranking and survival rewards", model_dir
                )
        except Exception:
            logger.exception("failed to load relevance judge from %s", model_dir)
        _load_attempted = True
        return _judge
