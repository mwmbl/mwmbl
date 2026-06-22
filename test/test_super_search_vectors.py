"""Unit tests for Super Search vector utilities (no DB / Redis needed)."""
import numpy as np

from mwmbl.tinysearchengine.super_search_select import vectors


DIM = 64


def test_projection_is_l2_normalised():
    vec = vectors.project_bow("the quick brown fox jumps", DIM)
    assert vec.shape == (DIM,)
    assert vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


def test_projection_is_deterministic():
    a = vectors.project_bow("python web framework", DIM)
    b = vectors.project_bow("python web framework", DIM)
    assert np.array_equal(a, b)


def test_empty_text_gives_zero_vector():
    vec = vectors.project_bow("", DIM)
    assert float(np.linalg.norm(vec)) == 0.0
    assert vectors.cosine(vec, vectors.project_bow("anything", DIM)) == 0.0


def test_stop_words_removed():
    # "the" / "of" are stop words, so these should project identically.
    with_stops = vectors.project_bow("the history of rome", DIM)
    without_stops = vectors.project_bow("history rome", DIM)
    assert np.allclose(with_stops, without_stops)


def test_cosine_self_is_one_and_bounds():
    vec = vectors.project_bow("machine learning models", DIM)
    assert abs(vectors.cosine(vec, vec) - 1.0) < 1e-5
    other = vectors.project_bow("medieval french poetry", DIM)
    sim = vectors.cosine(vec, other)
    assert -1.0 - 1e-6 <= sim <= 1.0 + 1e-6


def test_related_text_more_similar_than_unrelated():
    query = vectors.project_bow("django database migration", DIM)
    related = vectors.project_bow("django orm database schema migration", DIM)
    unrelated = vectors.project_bow("baroque opera composers", DIM)
    assert vectors.cosine(query, related) > vectors.cosine(query, unrelated)


def test_char_ngrams_match_on_shared_substrings():
    a = vectors.project_char_ngrams("tokenizer", DIM)
    b = vectors.project_char_ngrams("tokenize", DIM)
    c = vectors.project_char_ngrams("xylophone", DIM)
    assert vectors.cosine(a, b) > vectors.cosine(a, c)


def test_bytes_roundtrip():
    vec = vectors.project_bow("roundtrip test vector", DIM)
    restored = vectors.from_bytes(vectors.to_bytes(vec))
    assert np.array_equal(vec, restored)
    assert vectors.from_bytes(None) is None
    assert vectors.from_bytes(b"") is None


def test_query_cache_key_stable_and_short():
    k1 = vectors.query_cache_key("hello world")
    k2 = vectors.query_cache_key("hello world")
    assert k1 == k2 and len(k1) == 16
    assert vectors.query_cache_key("different") != k1
