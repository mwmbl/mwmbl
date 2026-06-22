"""Tests for Super Search source selection: profiles, features, policy."""
import fakeredis
import numpy as np
import pytest

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.super_search_select import features, policy, profiles, rewards, vectors
from mwmbl.tinysearchengine.super_search_select.features import QueryContext, SiteStats
from mwmbl.tinysearchengine.super_search_select.registry import SiteMeta
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext, compute_rewards

DIM = 64


@pytest.fixture
def fake_profile_redis(monkeypatch):
    """Binary-safe fake Redis wired into the profiles module."""
    r = fakeredis.FakeRedis()  # decode_responses=False (vectors are raw bytes)
    monkeypatch.setattr(profiles, "_redis", r)
    return r


def _doc(title, extract=""):
    return Document(title=title, url=f"https://x/{title}", extract=extract)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def test_unseen_profile_is_none(fake_profile_redis):
    assert profiles.get_profile("nope") == (None, None)


def test_update_then_get_profile(fake_profile_redis):
    profiles.update_profile("site", [_doc("python web framework", "django flask")])
    bow, cng = profiles.get_profile("site")
    assert bow is not None and cng is not None
    assert abs(float(np.linalg.norm(bow)) - 1.0) < 1e-5


def test_profile_moves_towards_repeated_topic(fake_profile_redis):
    # A site repeatedly returning ML content should profile closer to an ML query
    # than to an unrelated one.
    for _ in range(5):
        profiles.update_profile("ml", [_doc("machine learning neural networks", "deep models")])
    bow, _ = profiles.get_profile("ml")
    ml_q = vectors.project_bow("neural network training", DIM)
    other_q = vectors.project_bow("medieval french poetry", DIM)
    assert vectors.cosine(ml_q, bow) > vectors.cosine(other_q, bow)


def test_empty_docs_no_profile(fake_profile_redis):
    profiles.update_profile("site", [])
    assert profiles.get_profile("site") == (None, None)


def test_query_vectors_cached(fake_profile_redis):
    bow1, cng1 = profiles.get_query_vectors("hello world")
    bow2, cng2 = profiles.get_query_vectors("hello world")
    assert np.array_equal(bow1, bow2) and np.array_equal(cng1, cng2)
    # Cached under the query key.
    assert fake_profile_redis.exists(profiles._QVEC_BOW.format(key=vectors.query_cache_key("hello world")))


def test_batch_profiles(fake_profile_redis):
    profiles.update_profile("a", [_doc("alpha")])
    got = profiles.get_profiles(["a", "b"])
    assert got["a"][0] is not None
    assert got["b"] == (None, None)


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def _qctx(query):
    bow = vectors.project_bow(query, DIM)
    cng = vectors.project_char_ngrams(query, DIM)
    return QueryContext.build(query, bow, cng)


def test_feature_vector_shape_and_bias():
    meta = SiteMeta("s", "s.com", "tech", popularity=0.6, estimated_pages=0.6)
    x = features.feature_vector(_qctx("test query"), meta, (None, None))
    assert x.shape == (features.NUM_FEATURES,)
    assert x[0] == 1.0  # bias


def test_code_token_detected():
    assert _qctx("os.path.join example").has_code_token
    assert not _qctx("history of rome").has_code_token


def test_cosine_relevance_prefers_matching_profile():
    qctx = _qctx("django database migration")
    related = (vectors.project_bow("django orm migration schema", DIM),
               vectors.project_char_ngrams("django orm migration schema", DIM))
    unrelated = (vectors.project_bow("baroque opera composers", DIM),
                 vectors.project_char_ngrams("baroque opera composers", DIM))
    assert features.cosine_relevance(qctx, related) > features.cosine_relevance(qctx, unrelated)
    assert features.cosine_relevance(qctx, (None, None)) == 0.0


def test_feature_vector_uses_stats():
    meta = SiteMeta("s", "s.com", "tech")
    stats = SiteStats(contribution_ema=0.5, latency_ema=10.0, failure_rate=0.25)
    x = features.feature_vector(_qctx("q"), meta, (None, None), stats)
    names = features.FEATURE_NAMES
    assert x[names.index("contribution_ema")] == 0.5
    assert x[names.index("latency_penalty")] == 1.0  # clamped (latency >> timeout)
    assert x[names.index("failure_rate")] == 0.25


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def test_select_returns_all_when_fewer_than_k(fake_profile_redis):
    names = ["a", "b", "c"]
    assert set(policy.select_sources("q", names, k=10)) == set(names)


def test_select_includes_always_on(fake_profile_redis, monkeypatch):
    # mwmbl and hn are always-on in the static registry.
    names = ["mwmbl", "hn"] + [f"site{i}" for i in range(20)]
    chosen = policy.select_sources("python testing", names, k=5)
    assert "mwmbl" in chosen and "hn" in chosen
    assert len(chosen) == 5


def test_select_prefers_relevant_warm_site(fake_profile_redis, monkeypatch):
    monkeypatch.setattr("django.conf.settings.SUPER_SEARCH_EXPLORE_FLOOR", 0)
    # One warm site profiled on databases; others cold.
    for _ in range(3):
        profiles.update_profile("db_site", [_doc("postgres database sql indexing")])
    names = ["db_site"] + [f"cold{i}" for i in range(20)]
    chosen = policy.select_sources("sql database query", names, k=3)
    assert "db_site" in chosen


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------

def test_compute_rewards_fraction_in_top_k():
    ctx = SelectionContext(selected=["a", "b", "c"], per_source_limit=10)
    ctx.record_results("a", ["u1", "u2", "u3"])
    ctx.record_results("b", ["u4"])
    # c returned nothing useful
    final_top_k = ["u1", "u2", "u4"]  # a:2, b:1, c:0
    r = compute_rewards(ctx, final_top_k)
    assert r["a"] == pytest.approx(0.2)
    assert r["b"] == pytest.approx(0.1)
    assert r["c"] == 0.0


def test_compute_rewards_clamped_and_covers_all_selected():
    ctx = SelectionContext(selected=["a"], per_source_limit=2)
    ctx.record_results("a", ["u1", "u2", "u3"])
    r = compute_rewards(ctx, ["u1", "u2", "u3"])  # 3/2 -> clamp to 1.0
    assert r == {"a": 1.0}


def test_record_results_first_source_wins():
    ctx = SelectionContext(selected=["a", "b"], per_source_limit=10)
    ctx.record_results("a", ["shared"])
    ctx.record_results("b", ["shared"])
    assert ctx.source_by_url["shared"] == "a"


def test_log_impression_noop_without_database(monkeypatch):
    monkeypatch.setattr("django.conf.settings.HAS_DATABASE", False)
    # Should not raise even though no DB is configured.
    rewards.log_impression("q", SelectionContext(selected=["a"]), {"a": 0.5})
