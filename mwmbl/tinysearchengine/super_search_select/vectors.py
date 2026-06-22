"""Fixed-dimension text vectors for Super Search source selection.

Bag-of-words and character n-gram vectors are compressed to a fixed dimension
via *feature hashing* (the signed hashing trick). Feature hashing is itself a
random projection, so we never store or load a projection matrix — the
projection is defined entirely by the hash function (``mmh3``, already a project
dependency), which makes it deterministic and reproducible across processes.

Vectors are dense ``numpy`` float32 arrays, L2-normalised so cosine similarity
is a plain dot product. ``to_bytes`` / ``from_bytes`` give a compact Redis
representation.
"""
from __future__ import annotations

import hashlib

import mmh3
import numpy as np

from mwmbl.tokenizer import tokenize

# A compact English stop-word list. Removing these stops common function words
# from dominating the projected bag-of-words. (No stop-word list existed in the
# repo; this is intentionally small and self-contained.)
STOP_WORDS = frozenset({
    "a", "about", "above", "after", "again", "all", "am", "an", "and", "any",
    "are", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "did", "do", "does", "doing", "down",
    "during", "each", "few", "for", "from", "further", "had", "has", "have",
    "having", "he", "her", "here", "hers", "herself", "him", "himself", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
    "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off",
    "on", "once", "only", "or", "other", "our", "ours", "ourselves", "out",
    "over", "own", "s", "same", "she", "should", "so", "some", "such", "t",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "until", "up", "very", "was", "we", "were", "what", "when", "where",
    "which", "while", "who", "whom", "why", "will", "with", "you", "your",
    "yours", "yourself", "yourselves",
})

# Seeds for the two independent hashes used by the hashing trick: one picks the
# bucket, the other picks the sign. Independent seeds keep index and sign
# uncorrelated (a single hash split into index/sign couples the two).
_INDEX_SEED = 0
_SIGN_SEED = 1


def _hash_token(token: str, dim: int) -> tuple[int, float]:
    index = mmh3.hash(token, _INDEX_SEED, signed=False) % dim
    sign = 1.0 if (mmh3.hash(token, _SIGN_SEED, signed=False) & 1) else -1.0
    return index, sign


def _l2_normalise(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec


def _content_tokens(text: str, remove_stop_words: bool) -> list[str]:
    tokens = tokenize(text or "")
    if remove_stop_words:
        tokens = [t for t in tokens if t not in STOP_WORDS]
    return tokens


def project_bow(text: str, dim: int, *, remove_stop_words: bool = True) -> np.ndarray:
    """Project a bag-of-words of ``text`` into ``dim`` dimensions, L2-normalised."""
    vec = np.zeros(dim, dtype=np.float32)
    for token in _content_tokens(text, remove_stop_words):
        index, sign = _hash_token(token, dim)
        vec[index] += sign
    return _l2_normalise(vec)


def project_char_ngrams(
    text: str, dim: int, *, n_min: int = 3, n_max: int = 5, remove_stop_words: bool = True
) -> np.ndarray:
    """Project character ``n_min``..``n_max``-grams of ``text`` into ``dim`` dimensions.

    Each token is padded with spaces so word-boundary n-grams are captured
    (mirrors scikit-learn's ``char_wb`` analyzer). Robust to morphology, code
    identifiers and non-English text where whole-word BoW is brittle.
    """
    vec = np.zeros(dim, dtype=np.float32)
    for token in _content_tokens(text, remove_stop_words):
        padded = f" {token} "
        for n in range(n_min, n_max + 1):
            for i in range(len(padded) - n + 1):
                index, sign = _hash_token(padded[i:i + n], dim)
                vec[index] += sign
    return _l2_normalise(vec)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity of two L2-normalised vectors (a plain dot product)."""
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


def to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(raw: bytes | None) -> np.ndarray | None:
    if not raw:
        return None
    return np.frombuffer(raw, dtype=np.float32).copy()


def query_cache_key(query: str) -> str:
    """Stable short key for caching a query's projected vectors in Redis."""
    return hashlib.sha1(query.encode("utf-8", errors="ignore")).hexdigest()[:16]
