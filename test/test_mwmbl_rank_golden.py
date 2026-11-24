"""Characterisation tests for everything mwmbl_rank exports to Python.

These pin what the extension does today, odd behaviour included, so that rewriting its Rust
changes nothing by accident. A test that "documents current behaviour" does not endorse it:
change it deliberately, in the commit that changes the behaviour. Regenerate the larger
expected values in mwmbl_rank_golden.json with `uv run --no-sync python <this file>`.

After `maturin develop`, run these with `uv run --no-sync` too: a plain `uv run` can
reinstall a cached wheel of the extension over the fresh build.

Features and wiki scores are compared exactly, which assumes the same libm as the build
that wrote them; predictions get a tolerance, see test_predictions.
"""

import hashlib
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import pytest
from zstandard import ZstdCompressor, ZstdDecompressor

import mwmbl_rank
from mwmbl_rank import PageError, pack_index_page, read_index_page, write_index_page

GOLDEN_PATH = Path(__file__).with_name("mwmbl_rank_golden.json")

# (query, url, title, extract, score): unicode, empty fields, a URL query string, terms only
# in the path, a long query, punctuation, no match at all, an invalid URL.
RECORDS = [
    dict(zip(["query", "url", "title", "extract", "score"], fields))
    for fields in [
        ("rust programming", "https://www.rust-lang.org/learn", "Rust Programming Language", "A language empowering everyone to build reliable software.", 1.0),
        ("café crème", "https://fr.wikipedia.org/wiki/Café_crème", "Café crème — Wikipédia", "Le café crème est une boisson à base de café.", 0.5),
        ("machine learning", "https://example.com/ml", "", "", 0.0),
        ("search engine", "https://duckduckgo.com/?q=search+engine&ia=web", "DuckDuckGo", "", 0.3),
        ("graham essays", "https://paulgraham.com/graham/essays.html", "Home", "Welcome", 0.7),
        ("how to bake sourdough bread at home", "https://www.bbc.co.uk/food/recipes/sourdough_bread", "Sourdough bread recipe", "Bake a loaf of sourdough bread at home.", 2.5),
        ("Python's GIL?", "https://docs.python.org/3/glossary.html#term-global-interpreter-lock", "Glossary — Python 3 documentation", "global interpreter lock: the mechanism used by CPython.", -3.0),
        ("rust", "https://cooking.example.org/recipes", "Cooking Recipes", "bake bread", 1e6),
        ("local file", "not a url", "local file", "", 1.0),
        ("zebra crossing", "https://example.com/test", "Test", "test", 1.0),
    ]
]  # fmt: skip

# Applied to RECORDS in turn: Staan not asked, ranked first, Staan's own result, and asked
# but not returned (None, and NaN as pandas writes it).
PROVIDER_VARIANTS = [
    {},
    {"staan_asked": True, "staan_rank": 0},
    {"staan_asked": True, "staan_rank": 4, "from_staan": True},
    {"staan_asked": True, "staan_rank": None},
    {"staan_asked": True, "staan_rank": float("nan")},
]

# Arguments to RustXGBPipeline for the models whose predictions are pinned. The second sets
# every hyperparameter, so dropping any of them on the way to XGBoost changes its output: its
# max_depth stops trees that would grow deeper, and its threshold falls between two labels above 0.
MODEL_PARAMS = {
    "default": {"num_rounds": 20},
    "configured": dict(threshold=0.5, scale_pos_weight=1.0, reg_lambda=1.0, num_rounds=15, max_depth=1,
                       min_child_weight=0.5, gamma=0.1, subsample=0.8, provider_features=True),
}  # fmt: skip


def provider_records() -> list[dict]:
    return [record | PROVIDER_VARIANTS[i % len(PROVIDER_VARIANTS)] for i, record in enumerate(RECORDS)]


def training_data() -> tuple[list[dict], list[float], list[float]]:
    """Sixty rows over the golden records, with scores and labels varied arithmetically."""
    records = [
        provider_records()[i % len(RECORDS)]
        | {"score": (i * 7 % 11) / 5, "title": RECORDS[(i * 3) % len(RECORDS)]["title"]}
        for i in range(60)
    ]
    labels = [((i * 3) % 5 - 1) / 2 for i in range(60)]
    weights = [1.0 + i % 3 for i in range(60)]
    return records, labels, weights


def train(name: str) -> mwmbl_rank.RustXGBPipeline:
    records, labels, weights = training_data()
    pipeline = mwmbl_rank.RustXGBPipeline(**MODEL_PARAMS[name])
    pipeline.fit(records, labels, weights if name == "configured" else None)
    return pipeline


def golden_values() -> dict:
    """Everything mwmbl_rank_golden.json holds, computed by the extension as built."""
    return {
        "provider_features": mwmbl_rank.RustXGBPipeline.extract_features(provider_records(), True),
        "predictions": {name: train(name).predict(provider_records()) for name in MODEL_PARAMS},
    }


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def assert_same_floats(actual, expected):
    np.testing.assert_array_equal(np.asarray(actual, dtype=np.float64), np.asarray(expected, dtype=np.float64))


def features_of(record: dict, provider_features: bool = False) -> list[float]:
    return mwmbl_rank.RustXGBPipeline.extract_features([record], provider_features)[0]


def test_constants():
    assert mwmbl_rank.NUM_FEATURES == 50
    match_features = ["last_match_char", "match_length", "total_possible_match_length", "match_score", "match_terms"]
    fields = ["title", "extract", "domain", "domain_tokenized", "path", "query", "whole"]
    scalars = ["num_terms", "num_chars", "domain_score", "path_length", "domain_length", "wiki_score", "item_score"]
    assert mwmbl_rank.FEATURE_NAMES == [
        f"{feature}_{field}" for field in fields for feature in [*match_features, "match_term_proportion"]
    ] + [*scalars, "match_terms"]
    assert mwmbl_rank.PROVIDER_FEATURE_NAMES == ["in_staan", "staan_rank"]
    assert mwmbl_rank.RustXGBPipeline.feature_names() == mwmbl_rank.FEATURE_NAMES
    assert mwmbl_rank.RustXGBPipeline.num_features() == 50
    assert mwmbl_rank.PageError.__module__ == "mwmbl_rank"
    assert mwmbl_rank.PageError.__bases__ == (Exception,)


def test_extracted_features(golden):
    rows = mwmbl_rank.RustXGBPipeline.extract_features(provider_records(), True)
    assert_same_floats(rows, golden["provider_features"])
    shared = [row[:50] for row in golden["provider_features"]]
    assert_same_floats(mwmbl_rank.RustXGBPipeline.extract_features(RECORDS), shared)


def test_get_features_py_matches_extract_features_and_ignores_is_complete_and_term_case(golden):
    rows = [
        mwmbl_rank.get_features_py(r["query"].split(), r["title"], r["url"], r["extract"], r["score"], is_complete)
        for r, is_complete in zip(RECORDS, [True, False] * 5)
    ]
    assert_same_floats(rows, [row[:50] for row in golden["provider_features"]])
    record = RECORDS[0]
    args = (record["title"], record["url"], record["extract"], record["score"], True)
    assert mwmbl_rank.get_features_py(["RUST"], *args) == mwmbl_rank.get_features_py(["rust"], *args)


# Rather than bit-for-bit: libxgboost is a prebuilt binary whose float arithmetic may vary
# with the CPU it runs on. Here it is identical across runs and thread counts.
@pytest.mark.parametrize("name", MODEL_PARAMS)
def test_predictions(golden, name):
    predictions = train(name).predict(provider_records())
    np.testing.assert_allclose(predictions, golden["predictions"][name], rtol=1e-6, atol=1e-7)


@pytest.mark.parametrize("name", MODEL_PARAMS)
def test_too_few_matching_terms_zero_a_prediction_unless_staan_returned_the_record(golden, name):
    """Records 6 and 9 match at most half their query's terms. So do 2 and 7, but they are
    Staan's own results, which are exempt even for a model without provider features."""
    predictions = golden["predictions"][name]
    assert [i for i, prediction in enumerate(predictions) if prediction == 0.0] == [6, 9]
    assert predictions[2] > 0.0 and predictions[7] > 0.0


@pytest.mark.parametrize(
    "fields, kept",
    [
        ({"query": "rust cooking"}, False),
        ({"query": "rust cooking recipes"}, True),
        ({"from_staan": True}, True),
        ({"from_staan": np.bool_(True)}, True),
        ({"from_staan": 1}, False),
        ({"from_staan": "yes"}, False),
        ({"from_staan": None}, False),
    ],
)
def test_term_filter_boundary_and_from_staan_parsing(fields, kept):
    """Documents current behaviour: exactly half the terms is filtered, and from_staan must
    be a real bool to exempt a record."""
    prediction = train("default").predict([RECORDS[7] | fields])[0]
    assert (prediction > 0.0) == kept


def test_an_empty_query_has_infinite_match_scores_and_cannot_be_predicted():
    """Documents current behaviour: no terms divides by zero, and XGBoost refuses the inf."""
    record = RECORDS[0] | {"query": ""}
    infinite = [i for i, value in enumerate(features_of(record)) if math.isinf(value)]
    assert infinite == [3, 5, 9, 11, 15, 17, 21, 23, 27, 29, 33, 35, 39, 41]
    with pytest.raises(ValueError, match="Failed to create DMatrix"):
        train("default").predict([record])


def test_model_round_trips_through_a_file_with_its_provider_features_attribute(tmp_path):
    trained = train("configured")
    path = str(tmp_path / "model.json")
    trained.save_model(path)

    attributes = json.loads(Path(path).read_text())["learner"]["attributes"]
    loaded = mwmbl_rank.RustXGBPipeline()
    loaded.load_model(path)

    assert attributes["mwmbl_provider_features"] == "true"
    assert loaded.provider_features
    assert loaded.predict(provider_records()) == trained.predict(provider_records())


def test_a_model_without_the_provider_features_attribute_loads_without_them(tmp_path):
    path = tmp_path / "model.json"
    train("configured").save_model(str(path))
    model = json.loads(path.read_text())
    del model["learner"]["attributes"]["mwmbl_provider_features"]
    path.write_text(json.dumps(model))

    loaded = mwmbl_rank.RustXGBPipeline(provider_features=True)
    loaded.load_model(str(path))

    assert not loaded.provider_features


def test_saving_to_a_missing_directory_reports_xgboosts_error(tmp_path):
    path = tmp_path / "missing" / "model.json"
    with pytest.raises(
        ValueError,
        match=rf'^Failed to save model: XGBoost error: .*"{re.escape(str(path))}": No such file or directory',
    ):
        train("default").save_model(str(path))


def test_rust_repr_uses_rust_formatting():
    """Documents current behaviour, which differs from the Python wrapper's repr."""
    assert repr(mwmbl_rank.RustXGBPipeline()) == (
        "RustXGBPipeline(threshold=0, scale_pos_weight=0.1, reg_lambda=2, num_rounds=100, max_depth=None, "
        "min_child_weight=None, gamma=None, subsample=None, provider_features=false)"
    )
    assert repr(train("configured")) == (
        "RustXGBPipeline(threshold=0.5, scale_pos_weight=1, reg_lambda=1, num_rounds=15, max_depth=Some(1), "
        "min_child_weight=Some(0.5), gamma=Some(0.1), subsample=Some(0.8), provider_features=true)"
    )


def test_rust_fit_returns_none_and_negative_rounds_overflow():
    records, labels, _ = training_data()
    assert mwmbl_rank.RustXGBPipeline(num_rounds=1).fit(records, labels) is None
    with pytest.raises(OverflowError):
        mwmbl_rank.RustXGBPipeline(num_rounds=-1)


@pytest.mark.parametrize(
    "call, error, message",
    [
        (lambda p: p.fit([], []), ValueError, "No training records provided"),
        (lambda p: p.fit(RECORDS[:1], [1.0, 2.0]), ValueError, r"records length \(1\) != labels length \(2\)"),
        (lambda p: p.fit(RECORDS[:1], [1.0], [1.0, 2.0]), ValueError, r"weights length \(2\) != records length \(1\)"),
        (lambda p: p.predict(RECORDS), ValueError, r"Model has not been trained yet. Call fit\(\) first."),
        (lambda p: p.save_model("/nonexistent/model.xgb"), ValueError, "No trained model to save."),
        (
            lambda p: p.load_model("/nonexistent/model.xgb"),
            ValueError,
            "^Failed to load model: XGBoost error: File not found: /nonexistent/model.xgb$",
        ),
    ],
)
def test_pipeline_errors(call, error, message):
    with pytest.raises(error, match=message):
        call(mwmbl_rank.RustXGBPipeline(num_rounds=1))


@pytest.mark.parametrize(
    "record, error, message",
    [
        ({"url": "https://a.com/"}, ValueError, "Record missing 'query' key"),
        ({"query": "a"}, ValueError, "Record missing 'url' key"),
        ({"query": 5, "url": "https://a.com/"}, TypeError, ""),
        ({"query": "a", "url": None}, TypeError, ""),
        (1, ValueError, "Record missing 'query' key"),
        (["a"], ValueError, "Record missing 'query' key"),
    ],
)
def test_unreadable_record_errors(record, error, message):
    """Documents current behaviour: anything that is not a mapping is missing its query."""
    with pytest.raises(error, match=message):
        mwmbl_rank.RustXGBPipeline.extract_features([record])


def test_records_must_be_a_sequence_but_not_a_string():
    extract = mwmbl_rank.RustXGBPipeline.extract_features
    for records in [RECORDS[0], "records"]:
        with pytest.raises(TypeError):
            extract(records)
    assert extract(tuple(RECORDS[:1])) == extract(RECORDS[:1])


def test_missing_or_non_string_title_and_extract_are_read_as_empty():
    """Documents current behaviour: no error for a title or extract of the wrong type."""
    record = RECORDS[0]
    for field in ["title", "extract"]:
        empty = features_of(record | {field: ""})
        missing = features_of({key: value for key, value in record.items() if key != field})
        assert missing == empty
        assert features_of(record | {field: None}) == empty
        assert features_of(record | {field: 5}) == empty


def test_missing_or_non_numeric_score_is_read_as_zero():
    """Documents current behaviour: no error for a score of the wrong type."""
    record = RECORDS[0]
    zero = features_of(record | {"score": 0.0})
    assert features_of({key: value for key, value in record.items() if key != "score"}) == zero
    assert features_of(record | {"score": None}) == zero
    assert features_of(record | {"score": "high"}) == zero
    assert features_of(record | {"score": 3})[48] == 3.0
    assert features_of(record | {"score": np.float32(2.5)})[48] == 2.5


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"staan_asked": True, "staan_rank": 2.7}, [1.0, 2.0]),
        ({"staan_asked": True, "staan_rank": -1}, [1.0, 0.0]),
        ({"staan_asked": True, "staan_rank": 1e12}, [1.0, 4294967296.0]),
        ({"staan_asked": True, "staan_rank": "3"}, [0.0, math.nan]),
        ({"staan_asked": True}, [0.0, math.nan]),
        ({"staan_asked": np.bool_(True), "staan_rank": np.float64(3)}, [1.0, 3.0]),
        ({"staan_asked": 1, "staan_rank": 3}, [math.nan, math.nan]),
        ({"staan_asked": "yes", "staan_rank": 3}, [math.nan, math.nan]),
        ({"staan_asked": False, "staan_rank": 3}, [math.nan, math.nan]),
    ],
)
def test_staan_rank_is_truncated_saturated_and_lenient(fields, expected):
    """Documents current behaviour: a fractional rank truncates, an out-of-range one
    saturates to u32, a rank that is not a number counts as not returned, and staan_asked
    must be a real bool."""
    assert_same_floats(features_of(RECORDS[0] | fields, True)[50:], expected)


def test_wiki_score_reads_the_last_path_segment_of_any_url():
    """Documents current behaviour: the domain is never checked."""
    python = mwmbl_rank.wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)")
    assert python == 0.0009181497562013591
    assert mwmbl_rank.wiki_score("https://example.com/Python_(programming_language)") == python
    assert mwmbl_rank.wiki_score("Python_(programming_language)") == python
    assert mwmbl_rank.wiki_score("https://example.com/test") == 2.490051113958321e-05
    assert mwmbl_rank.wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)?x=1") == 0.0
    assert mwmbl_rank.wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)/") == 0.0
    assert mwmbl_rank.wiki_score("") == 0.0


# Python writes a document as a tuple and the crawler as an object, and a page can hold
# documents for other terms, untermed ones and short tuples. Dict keys come back sorted.
PAGE_ITEMS = [
    ["Café ☕", "https://example.com/?q=1", "Extract", 1.5, "term", None, 3],
    ("t", "u", "e", 2, "other"),
    ["x", "y", "z", 0.1],
    {"term": "term", "a": [1, True, None, -(2**63)]},
    {"no": "term"},
    {"term": 5},
]
PAGE_JSON = (
    '[["Café ☕","https://example.com/?q=1","Extract",1.5,"term",null,3],["t","u","e",2,"other"],'
    '["x","y","z",0.1],{"a":[1,true,null,-9223372036854775808],"term":"term"},{"no":"term"},{"term":5}]'
)
STORED_ITEMS = json.loads(PAGE_JSON)


def as_json(items) -> str:
    """Exact types, which == would blur: True == 1 and 2 == 2.0."""
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def test_packed_page_bytes():
    """The hash is of zstd's level-3 output, so a zstd upgrade may change it legitimately."""
    page, num_stored = pack_index_page(512, PAGE_ITEMS)

    assert num_stored == 6
    assert len(page) == 512
    assert ZstdDecompressor().decompress(page).decode() == PAGE_JSON
    assert hashlib.sha256(page).hexdigest() == "e72ea4dbacce13bcfa9fa5e4a8c1d21a3492cfc4ac4054fe597d33d3cae1c3ca"


def test_packing_json_number_formats_and_empty_pages():
    page, _ = pack_index_page(512, [[1.0, 0.1, 1e20, 1e-7, -0.0, True, 3]])
    assert ZstdDecompressor().decompress(page) == b"[[1.0,0.1,1e+20,1e-7,-0.0,true,3]]"
    empty, num_stored = pack_index_page(64, [])
    assert (ZstdDecompressor().decompress(empty), len(empty), num_stored) == (b"[]", 64, 0)
    assert pack_index_page(64, [["a" * 200], ["b"]])[1] == 2


@pytest.mark.parametrize(
    "item, message",
    [
        ([math.nan], "ValueError: NaN cannot be stored in an index page"),
        ([math.inf], "ValueError: inf cannot be stored in an index page"),
        ([2**63], "OverflowError"),
        ([{1}], "TypeError: Cannot store set in an index page"),
        ([b"x"], "TypeError: Cannot store bytes in an index page"),
        ({1: 2}, "TypeError: Only string keys can be stored in an index page"),
        (["\ud800"], "UnicodeEncodeError"),
    ],
)
def test_unstorable_items_are_page_errors(item, message):
    with pytest.raises(PageError, match=f"Cannot store these documents on a page: {message}"):
        pack_index_page(512, [item])


def test_page_size_and_argument_errors():
    with pytest.raises(PageError, match=r"Data is too big \(11\) for page size \(5\)"):
        pack_index_page(5, [])
    with pytest.raises(TypeError):
        pack_index_page(512, tuple(PAGE_ITEMS))


@pytest.fixture
def page_file(tmp_path):
    path = tmp_path / "pages"
    path.write_bytes(bytes(1024))
    descriptor = os.open(path, os.O_RDWR)
    yield descriptor
    os.close(descriptor)


def test_written_page_round_trips_and_filters_by_term(page_file):
    assert write_index_page(page_file, 512, 512, PAGE_ITEMS) == 6

    assert os.pread(page_file, 512, 512) == pack_index_page(512, PAGE_ITEMS)[0]
    assert as_json(read_index_page(page_file, 512, 512)) == PAGE_JSON
    assert as_json(read_index_page(page_file, 512, 512, "term")) == as_json([STORED_ITEMS[i] for i in [0, 2, 3, 4]])
    assert as_json(read_index_page(page_file, 512, 512, term="other")) == as_json([STORED_ITEMS[i] for i in [1, 2, 4]])


def test_a_page_holding_scalars_keeps_them_for_every_term(page_file):
    """Documents current behaviour: only a list or an object can belong to another term.
    The u64 is one the writer refuses but the reader accepts."""
    os.pwrite(page_file, ZstdCompressor().compress(b'[18446744073709551615,"x",[2.0,true]]'), 0)
    assert as_json(read_index_page(page_file, 0, 512, "x")) == '[18446744073709551615,"x",[2.0,true]]'


@pytest.mark.parametrize(
    "contents",
    [bytes(512), b"garbage!" * 8, ZstdCompressor().compress(b"not json"), ZstdCompressor().compress(b'{"a": 1}')],
)
def test_corrupt_pages_are_page_errors(page_file, contents):
    os.pwrite(page_file, contents, 0)
    with pytest.raises(PageError):
        read_index_page(page_file, 0, 512)


def test_short_reads_are_page_errors(page_file):
    for offset in [768, 4096]:
        with pytest.raises(PageError, match="could not read a whole page"):
            read_index_page(page_file, offset, 512)


def test_bad_descriptors_and_offsets(tmp_path):
    path = tmp_path / "pages"
    path.write_bytes(bytes(1024))
    read_only = os.open(path, os.O_RDONLY)
    with pytest.raises(OSError):
        write_index_page(read_only, 0, 512, [])
    with pytest.raises(OverflowError):
        read_index_page(read_only, -1, 512)
    os.close(read_only)
    with pytest.raises(OSError):
        read_index_page(read_only, 0, 512)


def test_descriptor_minus_one_panics():
    """Documents current behaviour: std's File::from_raw_fd asserts the descriptor is not
    -1, and pyo3 turns the panic into a PanicException, a BaseException not an Exception."""
    with pytest.raises(BaseException) as raised:
        read_index_page(-1, 0, 512)
    assert type(raised.value).__name__ == "PanicException"


if __name__ == "__main__":
    # One row to a line, so a change shows up in a diff as the rows it touched.
    values = golden_values()
    rows = ",\n".join(json.dumps(row) for row in values["provider_features"])
    predictions = json.dumps(values["predictions"])
    GOLDEN_PATH.write_text(f'{{\n"provider_features": [\n{rows}\n],\n"predictions": {predictions}\n}}\n')
