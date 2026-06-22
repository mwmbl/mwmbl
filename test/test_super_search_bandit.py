"""Tests for the linear-Gaussian Thompson-sampling source-selection bandit."""
import fakeredis
import numpy as np
import pytest

from mwmbl.tinysearchengine.super_search_select import bandit, policy, profiles
from mwmbl.tinysearchengine.super_search_select.features import NUM_FEATURES
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext


@pytest.fixture
def fake_bandit_redis(monkeypatch):
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(bandit, "_redis", r)
    return r


def test_prior_state_shape():
    s = bandit.prior_state()
    assert s.A.shape == (NUM_FEATURES, NUM_FEATURES)
    assert s.b.shape == (NUM_FEATURES,)
    # Prior is lambda*I, b=0 -> posterior mean 0.
    assert np.allclose(s.A, np.eye(NUM_FEATURES))
    assert np.allclose(s.b, 0.0)


def test_unseen_arm_returns_prior(fake_bandit_redis):
    states = bandit.get_states(["never_seen"])
    assert np.allclose(states["never_seen"].b, 0.0)


def test_state_roundtrip(fake_bandit_redis):
    s = bandit.prior_state()
    x = np.arange(NUM_FEATURES, dtype=np.float64)
    bandit.update_arm(s, x, 1.0)
    bandit.save_states({"site": s})
    loaded = bandit.get_states(["site"])["site"]
    assert np.allclose(loaded.A, s.A)
    assert np.allclose(loaded.b, s.b)


def test_update_changes_posterior_mean(fake_bandit_redis):
    # Repeatedly reward a feature direction; posterior mean should move that way.
    s = bandit.prior_state()
    x = np.zeros(NUM_FEATURES)
    x[1] = 1.0  # the cos_bow slot
    for _ in range(20):
        bandit.update_arm(s, x, 1.0)
    mean = np.linalg.inv(s.A) @ s.b
    assert mean[1] > 0.5


def test_update_via_features_and_rewards(fake_bandit_redis):
    feats = {"a": [0.0] * NUM_FEATURES, "b": [0.0] * NUM_FEATURES}
    feats["a"][1] = 1.0
    feats["b"][1] = 1.0
    bandit.update(feats, {"a": 1.0, "b": 0.0})
    sa = bandit.get_states(["a"])["a"]
    sb = bandit.get_states(["b"])["b"]
    # 'a' got reward 1 in slot 1, 'b' got 0 -> a's b-vector larger there.
    assert sa.b[1] == pytest.approx(1.0)
    assert sb.b[1] == pytest.approx(0.0)


def test_all_sites_and_export(fake_bandit_redis):
    bandit.update({"a": [1.0] * NUM_FEATURES, "b": [1.0] * NUM_FEATURES},
                  {"a": 1.0, "b": 0.5})
    assert set(bandit.all_sites()) == {"a", "b"}
    exported = bandit.export_all()
    assert set(exported) == {"a", "b"}


def test_seed_states_does_not_clobber_live(fake_bandit_redis):
    # Live arm trained; a stale Postgres prior should not overwrite it.
    s = bandit.prior_state()
    x = np.zeros(NUM_FEATURES); x[1] = 1.0
    for _ in range(10):
        bandit.update_arm(s, x, 1.0)
    bandit.save_states({"site": s})

    bandit.seed_states({"site": bandit.prior_state()}, overwrite=False)
    loaded = bandit.get_states(["site"])["site"]
    assert not np.allclose(loaded.b, 0.0)  # live state preserved


def test_thompson_prefers_learned_good_arm(fake_bandit_redis):
    # Train arm "good" to like a context, "bad" to dislike it; sampling should
    # pick "good" most of the time.
    x = np.zeros(NUM_FEATURES)
    x[0], x[1] = 1.0, 1.0  # bias + cos_bow
    good = bandit.prior_state()
    bad = bandit.prior_state()
    for _ in range(50):
        bandit.update_arm(good, x, 1.0)
        bandit.update_arm(bad, x, 0.0)
    bandit.save_states({"good": good, "bad": bad})

    rng = np.random.default_rng(0)
    states = bandit.get_states(["good", "bad"])
    wins = 0
    for _ in range(200):
        sg = bandit.sample_score(states["good"], x, rng)
        sb = bandit.sample_score(states["bad"], x, rng)
        wins += sg > sb
    assert wins > 150  # good clearly preferred


def test_policy_bandit_branch_selects_and_records_features(monkeypatch):
    """End-to-end: USE_BANDIT selection populates ctx.features and respects k."""
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(bandit, "_redis", r)
    monkeypatch.setattr(profiles, "_redis", r)
    monkeypatch.setattr("django.conf.settings.SUPER_SEARCH_USE_BANDIT", True)

    names = ["mwmbl", "hn"] + [f"site{i}" for i in range(20)]
    ctx = SelectionContext()
    chosen = policy.select_sources("python testing tools", names, k=5, ctx=ctx)

    assert len(chosen) == 5
    assert "mwmbl" in chosen and "hn" in chosen  # always-on included
    # A feature vector was recorded for every chosen source.
    assert set(chosen) <= set(ctx.features)
    assert all(len(v) == NUM_FEATURES for v in ctx.features.values())
