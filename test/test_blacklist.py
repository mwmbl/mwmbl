from mwmbl.indexer.blacklist import is_domain_blacklisted


def test_blacklist_excludes_bad_pattern():
    bad_domains = [
        "brofqpxj.uelinc.com",
        "gwaspsag.enflightmultisport.com",
        "fmcqgzvk.onlinejobs2day.com",
        "btmjmhyj.universityslandown.com",
        "djqfctsq.ropman.com",
    ]

    for domain in bad_domains:
        assert is_domain_blacklisted(domain, set())


def test_blacklist_allows_top_domains():
    assert not is_domain_blacklisted("teamblog.supportbee.com", set())


def test_blacklist_allows_other_domains():
    assert not is_domain_blacklisted("something.com", set())
