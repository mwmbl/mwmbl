"""
Parity tests: compare Rust feature extraction output against the Python reference.

These tests require the mwmbl_rank Rust extension to be built:
    maturin develop

Run with:
    pytest test/test_rust_features.py -v
"""
import math
import pytest

# Skip the entire module if the Rust extension is not built
mwmbl_rank = pytest.importorskip("mwmbl_rank", reason="mwmbl_rank Rust extension not built")

from mwmbl.tinysearchengine.rank import get_features as py_get_features

# Tolerance for floating-point comparison between Python and Rust
ATOL = 1e-4


def rust_get_features(terms, title, url, extract, score, is_complete):
    """Call the Rust get_features_py function."""
    return mwmbl_rank.get_features_py(
        list(terms), title, url, extract, float(score), is_complete
    )


# ---------------------------------------------------------------------------
# Test cases: (terms, title, url, extract, score, is_complete)
# ---------------------------------------------------------------------------
CASES = [
    (
        ["rust", "programming"],
        "Rust Programming Language",
        "https://www.rust-lang.org/",
        "A systems programming language focused on safety and performance.",
        1.0,
        True,
    ),
    (
        ["python"],
        "Python (programming language)",
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "Python is a high-level, general-purpose programming language.",
        0.5,
        True,
    ),
    (
        ["search", "engine"],
        "Mwmbl Search Engine",
        "https://mwmbl.org/",
        "A free, open-source search engine.",
        0.8,
        False,
    ),
    (
        ["django"],
        "Django Web Framework",
        "https://www.djangoproject.com/",
        "The web framework for perfectionists with deadlines.",
        0.3,
        True,
    ),
    (
        ["machine", "learning"],
        "",
        "https://example.com/ml",
        "",
        0.0,
        True,
    ),
    (
        ["xgboost"],
        "XGBoost Documentation",
        "https://xgboost.readthedocs.io/en/stable/",
        "XGBoost is an optimized distributed gradient boosting library.",
        0.9,
        True,
    ),
    (
        ["paul", "graham"],
        "Paul Graham Essays",
        "https://paulgraham.com/articles.html",
        "Essays by Paul Graham on startups and technology.",
        0.7,
        True,
    ),
    (
        ["rust"],
        "Rust Blog",
        "https://blog.rust-lang.org/2024/01/01/release.html",
        "The Rust programming language blog.",
        0.6,
        False,
    ),
    # Edge case: empty terms
    (
        [],
        "Some Title",
        "https://example.com/",
        "Some extract text.",
        0.5,
        True,
    ),
    # Edge case: URL with no domain match
    (
        ["test"],
        "Test Page",
        "https://totally-unknown-xyz123.example.com/test/page?q=foo",
        "A test page.",
        0.1,
        True,
    ),
]


@pytest.mark.parametrize("terms,title,url,extract,score,is_complete", CASES)
def test_feature_parity(terms, title, url, extract, score, is_complete):
    """Rust and Python feature vectors should agree to within ATOL."""
    py_feats = py_get_features(terms, title, url, extract, score, is_complete)
    rust_feats = rust_get_features(terms, title, url, extract, score, is_complete)

    assert len(rust_feats) == mwmbl_rank.NUM_FEATURES, (
        f"Expected {mwmbl_rank.NUM_FEATURES} features, got {len(rust_feats)}"
    )
    assert len(py_feats) == len(rust_feats), (
        f"Feature count mismatch: Python={len(py_feats)}, Rust={len(rust_feats)}"
    )

    feature_names = mwmbl_rank.FEATURE_NAMES
    mismatches = []
    for i, (py_val, rust_val) in enumerate(zip(py_feats.values(), rust_feats)):
        if not math.isclose(float(py_val), float(rust_val), abs_tol=ATOL, rel_tol=1e-3):
            mismatches.append(
                f"  [{i}] {feature_names[i]}: Python={py_val:.6f}, Rust={rust_val:.6f}"
            )

    assert not mismatches, (
        f"Feature mismatches for query={terms!r}, url={url!r}:\n" + "\n".join(mismatches)
    )


def test_feature_names_match():
    """Rust and Python feature names should be in the same order."""
    rust_names = list(mwmbl_rank.FEATURE_NAMES)
    # Get Python feature names by running get_features and checking dict keys
    py_feats = py_get_features(["test"], "Test", "https://example.com/", "Extract", 1.0, True)
    py_names = list(py_feats.keys())

    assert rust_names == py_names, (
        f"Feature name mismatch.\n"
        f"Rust: {rust_names}\n"
        f"Python: {py_names}"
    )


def test_num_features():
    """NUM_FEATURES constant should be 50."""
    assert mwmbl_rank.NUM_FEATURES == 50


def test_no_nan_in_features():
    """No feature should be NaN for any test case."""
    for terms, title, url, extract, score, is_complete in CASES:
        rust_feats = rust_get_features(terms, title, url, extract, score, is_complete)
        for i, val in enumerate(rust_feats):
            assert not math.isnan(val), (
                f"NaN at feature {i} ({mwmbl_rank.FEATURE_NAMES[i]}) "
                f"for query={terms!r}, url={url!r}"
            )


def test_domain_score_known_domain():
    """paulgraham.com is in the HN top domains list; domain_score should be > 0."""
    rust_feats = rust_get_features(
        ["paul"], "Paul Graham", "https://paulgraham.com/articles.html", "", 1.0, True
    )
    feature_names = list(mwmbl_rank.FEATURE_NAMES)
    domain_score_idx = feature_names.index("domain_score")
    assert rust_feats[domain_score_idx] > 0.0


def test_wiki_score_zero_for_non_wiki():
    """A non-Wikipedia URL should have wiki_score = 0."""
    rust_feats = rust_get_features(
        ["test"], "Test", "https://example.com/test", "", 1.0, True
    )
    feature_names = list(mwmbl_rank.FEATURE_NAMES)
    wiki_score_idx = feature_names.index("wiki_score")
    assert rust_feats[wiki_score_idx] == 0.0
