import fakeredis
import numpy as np
import pytest

from mwmbl.indexer import blacklist_snapshot
from mwmbl.indexer.blacklist_snapshot import (
    SNAPSHOT_KEY,
    SNAPSHOT_VERSION_KEY,
    SnapshotBlacklist,
    build_snapshot,
    collect_remote_domains,
    publish_snapshot,
    refresh_snapshot,
)
from mwmbl.indexer.blacklist_providers import (
    CombinedBlacklistProvider,
    RemoteListBlacklistProvider,
    StaticBlacklistProvider,
)


class FakeRemoteProvider(RemoteListBlacklistProvider):
    """A remote list provider that serves a fixed set instead of fetching."""

    def __init__(self, domains):
        super().__init__(f"http://fake/{id(self)}")
        self._domains = set(domains)

    def _get_blacklisted_domains(self):
        return self._domains


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


@pytest.fixture
def provider():
    return CombinedBlacklistProvider([
        StaticBlacklistProvider({"not-remote.test"}),
        FakeRemoteProvider({"badsite.test", "evil.test"}),
        FakeRemoteProvider({"evil.test", "spam.test"}),
    ])


def test_collect_remote_domains_walks_combined_providers_and_skips_local_ones(provider):
    # Only remote lists are snapshotted; local rule providers are evaluated in-process,
    # so "not-remote.test" must not appear here.
    assert collect_remote_domains(provider) == {"badsite.test", "evil.test", "spam.test"}


def test_build_snapshot_is_sorted_and_deduplicated(provider):
    array = np.frombuffer(build_snapshot(provider), dtype=blacklist_snapshot.HASH_DTYPE)
    assert len(array) == 3  # evil.test appears in both lists
    assert list(array) == sorted(array)


def test_publish_and_load_round_trip(provider, redis_client):
    version = refresh_snapshot(provider, redis_client)

    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    assert blacklist.load_now() is True
    assert blacklist.filter_blacklisted(
        ["badsite.test", "evil.test", "spam.test", "good.test"]
    ) == {"badsite.test", "evil.test", "spam.test"}
    assert redis_client.get(SNAPSHOT_VERSION_KEY).decode() == version


def test_load_is_a_no_op_when_the_version_is_unchanged(provider, redis_client):
    refresh_snapshot(provider, redis_client)
    blacklist = SnapshotBlacklist(redis_client=redis_client)
    assert blacklist.load_now() is True
    assert blacklist.load_now() is False


def test_load_picks_up_a_republished_snapshot(redis_client):
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)

    refresh_snapshot(CombinedBlacklistProvider([FakeRemoteProvider({"first.test"})]), redis_client)
    blacklist.load_now()
    assert blacklist.filter_blacklisted(["first.test", "second.test"]) == {"first.test"}

    refresh_snapshot(CombinedBlacklistProvider([FakeRemoteProvider({"second.test"})]), redis_client)
    assert blacklist.load_now() is True
    assert blacklist.filter_blacklisted(["first.test", "second.test"]) == {"second.test"}


def test_built_in_rules_apply_without_a_snapshot(redis_client):
    """The local rules must work even before the first snapshot lands, so a domain added
    to mwmbl/settings.py takes effect on deploy rather than at the next refresh."""
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider({"local.test"}),
                                  redis_client=redis_client)

    assert blacklist.loaded is False
    assert blacklist.filter_blacklisted(["local.test", "good.test"]) == {"local.test"}


def test_eviction_keeps_the_loaded_snapshot(provider, redis_client):
    """Production Redis runs allkeys-lru, so the snapshot can vanish. Losing it must not
    silently disable filtering."""
    refresh_snapshot(provider, redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()

    redis_client.delete(SNAPSHOT_KEY, SNAPSHOT_VERSION_KEY)

    assert blacklist.load_now() is False
    assert blacklist.filter_blacklisted(["badsite.test", "good.test"]) == {"badsite.test"}


def test_redis_failure_keeps_the_loaded_snapshot(provider):
    class BrokenRedis:
        def get(self, key):
            raise ConnectionError("redis is down")

    refresh_snapshot(provider, (working := fakeredis.FakeRedis()))
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=working)
    blacklist.load_now()

    blacklist._redis = BrokenRedis()
    assert blacklist.load_now() is False
    assert blacklist.filter_blacklisted(["badsite.test"]) == {"badsite.test"}


def test_publishing_the_same_domains_twice_keeps_the_same_version(provider, redis_client):
    """Workers skip the 11 MB download when the version is unchanged, so an unchanged
    blocklist must not churn the version."""
    assert refresh_snapshot(provider, redis_client) == refresh_snapshot(provider, redis_client)


def test_filter_blacklisted_handles_an_empty_input(provider, redis_client):
    blacklist = SnapshotBlacklist(redis_client=redis_client)
    publish_snapshot(build_snapshot(provider), redis_client)
    blacklist.load_now()

    assert blacklist.filter_blacklisted([]) == set()


def test_is_domain_blacklisted(provider, redis_client):
    refresh_snapshot(provider, redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()

    assert blacklist.is_domain_blacklisted("badsite.test") is True
    assert blacklist.is_domain_blacklisted("good.test") is False


def test_hashes_above_every_entry_do_not_index_out_of_bounds(redis_client):
    """np.searchsorted returns len(array) for a hash larger than everything in it."""
    refresh_snapshot(CombinedBlacklistProvider([FakeRemoteProvider({"only.test"})]), redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()

    # Try enough domains that at least one hashes above and one below the single entry.
    candidates = [f"candidate{i}.test" for i in range(100)]
    assert blacklist.filter_blacklisted(candidates) == set()


# ---------------------------------------------------------------------------
# Subdomain matching
# ---------------------------------------------------------------------------
#
# The remote lists are apex rules - HaGeZi's file header says "Syntax: Domains (without
# subdomains)" - so an entry has to cover the subdomains of that domain too, starting
# with the www. form most sites actually serve.

@pytest.fixture
def apex_blacklist(redis_client):
    refresh_snapshot(CombinedBlacklistProvider([FakeRemoteProvider({"badsite.test"})]), redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()
    return blacklist


@pytest.mark.parametrize("domain", [
    "badsite.test",
    "www.badsite.test",
    "images.badsite.test",
    "a.deeply.nested.badsite.test",
])
def test_an_apex_entry_covers_its_subdomains(apex_blacklist, domain):
    assert apex_blacklist.is_domain_blacklisted(domain) is True


@pytest.mark.parametrize("domain", [
    "badsite.test.example.com",   # the entry is a prefix, not a suffix
    "notbadsite.test",            # suffix of the string, but not a parent domain
    "example.test",               # shares only the TLD
])
def test_an_apex_entry_does_not_cover_unrelated_domains(apex_blacklist, domain):
    assert apex_blacklist.is_domain_blacklisted(domain) is False


def test_a_tld_is_never_a_candidate(redis_client):
    """Matching down to a bare TLD would take out every domain under it."""
    refresh_snapshot(CombinedBlacklistProvider([FakeRemoteProvider({"test"})]), redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()

    assert blacklist.is_domain_blacklisted("innocent.test") is False


def test_a_truncated_blob_is_ignored_rather_than_raising(provider, redis_client):
    """load_now() runs at import time via search_setup, so raising here would stop every
    web worker from starting."""
    refresh_snapshot(provider, redis_client)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=redis_client)
    blacklist.load_now()
    assert blacklist.is_domain_blacklisted("badsite.test") is True

    redis_client.set(SNAPSHOT_KEY, redis_client.get(SNAPSHOT_KEY)[:-3])
    redis_client.set(SNAPSHOT_VERSION_KEY, "a-new-version")

    assert blacklist.load_now() is False
    # The good array it already had is kept rather than being replaced by a bad one.
    assert blacklist.is_domain_blacklisted("badsite.test") is True
