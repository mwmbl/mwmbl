"""Tests for the Pass-2 pool merge rule and the Staan augmentation step.

The merge rule decides what a training row's features are for a URL more than one source
returned, so getting it wrong shifts features rather than raising - which is exactly the kind
of mistake that only shows up as a model that underperforms for no visible reason.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._relabel_pool import add  # noqa: E402

# ---------------------------------------------------------------------------
# The merge rule
# ---------------------------------------------------------------------------


def test_a_new_url_enters_the_pool():
    pool = {}

    add(pool, "https://tokio.rs/", "Tokio", "An async runtime.", None, 6.0, pool_tag="staan", ss_source="staan")

    assert pool["https://tokio.rs/"] == {
        "url": "https://tokio.rs/",
        "title": "Tokio",
        "extract": "An async runtime.",
        "state": None,
        "score": 6.0,
        "pools": ["staan"],
        "ss_source": "staan",
        "gold_rank": None,
    }


def test_a_url_with_no_url_is_ignored():
    pool = {}

    add(pool, "", "Tokio", "", None, 6.0, pool_tag="staan")

    assert pool == {}


def test_provenance_accumulates_across_sources():
    """`pools` is what the dataset's calibration readout groups by, so a URL two sources both
    returned has to say so rather than claim the first one only."""
    pool = {}

    add(pool, "https://tokio.rs/", "Tokio", "", None, 2.0, pool_tag="standard")
    add(pool, "https://tokio.rs/", "Tokio", "", None, 6.0, pool_tag="staan", ss_source="staan")

    assert pool["https://tokio.rs/"]["pools"] == ["standard", "staan"]


def test_the_first_score_wins():
    """A URL two sources both returned keeps the score of whichever was pooled first. Every
    row in the existing dataset was built this way; changing it here would make the new Staan
    rows incomparable with the ones already judged."""
    pool = {}

    add(pool, "https://tokio.rs/", "Tokio", "", None, 2.0, pool_tag="standard")
    add(pool, "https://tokio.rs/", "Tokio", "", None, 6.0, pool_tag="staan")

    assert pool["https://tokio.rs/"]["score"] == 2.0


def test_the_richest_title_and_extract_win():
    pool = {}

    add(pool, "https://tokio.rs/", "Tokio", "Short.", None, 1.0, pool_tag="standard")
    add(pool, "https://tokio.rs/", "Tokio - async Rust", "A much longer description.", None, 1.0, pool_tag="staan")

    assert pool["https://tokio.rs/"]["title"] == "Tokio - async Rust"
    assert pool["https://tokio.rs/"]["extract"] == "A much longer description."


def test_a_repeated_pool_tag_is_not_duplicated():
    pool = {}

    add(pool, "https://tokio.rs/", "Tokio", "", None, 1.0, pool_tag="staan")
    add(pool, "https://tokio.rs/", "Tokio", "", None, 1.0, pool_tag="staan")

    assert pool["https://tokio.rs/"]["pools"] == ["staan"]


# ---------------------------------------------------------------------------
# The augmentation step
# ---------------------------------------------------------------------------


@pytest.fixture
def relabel_files(tmp_path, monkeypatch):
    """Point the augment script at a throwaway pool and checkpoint."""
    augment = pytest.importorskip("scripts.llm_relabel_pass2_augment_staan")

    pool = tmp_path / "pass2_pool.jsonl"
    checkpoint = tmp_path / "pass2_staan.jsonl"
    monkeypatch.setattr(augment, "POOL", str(pool))
    monkeypatch.setattr(augment, "CHECKPOINT", str(checkpoint))
    return augment, pool, checkpoint


def _write(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_merge_adds_staan_candidates_to_the_pool(relabel_files):
    augment, pool, checkpoint = relabel_files
    _write(
        pool,
        [
            {
                "query": "tokio",
                "sources": ["mwmbl"],
                "candidates": [
                    {
                        "url": "https://blog.example.com/tokio",
                        "title": "A crawled page",
                        "extract": "",
                        "state": None,
                        "score": 1.0,
                        "pools": ["standard"],
                        "ss_source": None,
                        "gold_rank": None,
                    }
                ],
            }
        ],
    )
    _write(
        checkpoint,
        [{"query": "tokio", "results": [{"url": "https://tokio.rs/", "title": "Tokio", "extract": "", "score": 6.0}]}],
    )

    augment.cmd_merge()

    candidates = json.loads(pool.read_text().strip())["candidates"]
    assert [c["url"] for c in candidates] == ["https://blog.example.com/tokio", "https://tokio.rs/"]
    assert candidates[1]["pools"] == ["staan"]
    assert candidates[1]["ss_source"] == "staan"
    assert candidates[1]["score"] == 6.0


def test_merge_folds_a_url_another_source_already_returned(relabel_files):
    augment, pool, checkpoint = relabel_files
    _write(
        pool,
        [
            {
                "query": "tokio",
                "sources": ["mwmbl"],
                "candidates": [
                    {
                        "url": "https://tokio.rs/",
                        "title": "Tokio",
                        "extract": "",
                        "state": None,
                        "score": 2.0,
                        "pools": ["standard"],
                        "ss_source": None,
                        "gold_rank": None,
                    }
                ],
            }
        ],
    )
    _write(
        checkpoint,
        [{"query": "tokio", "results": [{"url": "https://tokio.rs/", "title": "Tokio", "extract": "", "score": 6.0}]}],
    )

    augment.cmd_merge()

    candidates = json.loads(pool.read_text().strip())["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["pools"] == ["standard", "staan"]
    assert candidates[0]["score"] == 2.0


def test_merge_leaves_queries_with_no_staan_record_alone(relabel_files):
    augment, pool, checkpoint = relabel_files
    original = {
        "query": "untouched",
        "sources": ["mwmbl"],
        "candidates": [
            {
                "url": "https://example.com/",
                "title": "T",
                "extract": "",
                "state": None,
                "score": 1.0,
                "pools": ["standard"],
                "ss_source": None,
                "gold_rank": None,
            }
        ],
    }
    _write(pool, [original])
    _write(checkpoint, [{"query": "something else", "results": []}])

    augment.cmd_merge()

    assert json.loads(pool.read_text().strip()) == original


def test_merge_is_idempotent(relabel_files):
    """Re-running the merge must not double-count: the checkpoint stays on disk after a
    merge, and the obvious mistake is to run it twice."""
    augment, pool, checkpoint = relabel_files
    _write(
        pool,
        [{"query": "tokio", "sources": [], "candidates": []}],
    )
    _write(
        checkpoint,
        [{"query": "tokio", "results": [{"url": "https://tokio.rs/", "title": "Tokio", "extract": "", "score": 6.0}]}],
    )

    augment.cmd_merge()
    augment.cmd_merge()

    candidates = json.loads(pool.read_text().strip())["candidates"]
    assert [c["url"] for c in candidates] == ["https://tokio.rs/"]
    assert candidates[0]["pools"] == ["staan"]
