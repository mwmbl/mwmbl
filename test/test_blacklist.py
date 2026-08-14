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


def test_blacklist_excludes_adult_sites_found_via_search_console():
    """Domains surfaced by suspicious Search Console queries (2026-08-14)."""
    provider = BuiltInRulesBlacklistProvider()
    bad_domains = [
        "fineartteens.com",
        "milforia.com",
        "azgals.com",
        "kusowanka.com",
        "nyou.booru.org",
        "hypno.booru.org",
        "inflatebooru.booru.org",
        "futabooru.booru.org",
        "captions.booru.org",
    ]
    for domain in bad_domains:
        assert provider.is_domain_blacklisted(domain), domain


def test_blacklist_allows_legitimate_imageboard():
    """mathchan.org ('the scientific imageboard') is not adult content."""
    provider = BuiltInRulesBlacklistProvider()
    assert not provider.is_domain_blacklisted("mathchan.org")
