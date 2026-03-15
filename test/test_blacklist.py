from mwmbl.indexer.blacklist_providers import BuiltInRulesBlacklistProvider


def test_blacklist_excludes_bad_pattern():
    """Test that built-in rules blacklist bad patterns."""
    provider = BuiltInRulesBlacklistProvider()
    bad_domains = [
        "brofqpxj.uelinc.com",
        "gzsmjc.fba01.com", 
        "59648.etnomurcia.com",
        "something.hzqwyou.cn",
    ]

    for domain in bad_domains:
        assert provider.is_domain_blacklisted(domain)


def test_blacklist_allows_top_domains():
    """Test that built-in rules allow legitimate domains."""
    provider = BuiltInRulesBlacklistProvider()
    assert not provider.is_domain_blacklisted("teamblog.supportbee.com")


def test_blacklist_allows_other_domains():
    """Test that built-in rules allow other legitimate domains."""
    provider = BuiltInRulesBlacklistProvider()
    assert not provider.is_domain_blacklisted("something.com")
