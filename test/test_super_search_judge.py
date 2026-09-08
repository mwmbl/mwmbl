"""Tests for the fine-tuned relevance judge (super_search_select/judge.py)."""

from pathlib import Path

import pytest
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select import judge as ss_judge

MODEL_DIR = Path(getattr(settings, "SUPER_SEARCH_JUDGE_MODEL_DIR", ""))


def test_doc_text_matches_training_format():
    assert ss_judge.doc_text("Title", "Extract") == "Title. Extract"
    assert ss_judge.doc_text(None, "Extract") == ". Extract"
    assert ss_judge.doc_text("Title", None) == "Title."
    assert len(ss_judge.doc_text("t" * 2000, "e")) == ss_judge.DOC_CHARS


def test_get_judge_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(ss_judge, "_judge", None)
    monkeypatch.setattr(ss_judge, "_load_attempted", False)
    monkeypatch.setattr(settings, "SUPER_SEARCH_JUDGE_MODEL_DIR", "/nonexistent")
    assert ss_judge.get_judge() is None
    # memoized: second call must not retry the load
    assert ss_judge._load_attempted
    assert ss_judge.get_judge() is None


@pytest.mark.skipif(not (MODEL_DIR / "model.onnx").exists(), reason="judge model artifact not present")
def test_judge_scores_relevant_above_irrelevant():
    judge = ss_judge.Judge(MODEL_DIR)
    scores = judge.score(
        "python asyncio tutorial",
        [
            ss_judge.doc_text(
                "Async IO in Python: A Complete Walkthrough", "A tutorial covering asyncio, coroutines and event loops."
            ),
            ss_judge.doc_text("Best chocolate cake recipes", "Moist chocolate cake with simple ingredients."),
        ],
    )
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores[0] > scores[1]
