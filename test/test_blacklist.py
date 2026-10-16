from mwmbl.indexer.blacklist_providers import BuiltInRulesBlacklistProvider


def test_blacklist_excludes_bad_pattern():
    """Test that built-in rules blacklist bad patterns."""
    provider = BuiltInRulesBlacklistProvider()
    bad_domains = [
        "59648.etnomurcia.com",
        "something.hzqwyou.cn",
    ]

    for domain in bad_domains:
        assert provider.is_domain_blacklisted(domain)


def test_blacklist_allows_numeric_apex_domain():
    """A numeric *apex* domain like 350.org is the actual site name, not a generated
    subdomain - the numeric-subdomain heuristic below should only catch len > 2."""
    provider = BuiltInRulesBlacklistProvider()
    assert not provider.is_domain_blacklisted("350.org")


def test_blacklist_allows_ordinary_short_subdomains():
    """Regression test: a length-based heuristic that used to flag any 3-part .com
    domain with a 6 or 8 character first label was removed after it flagged dozens of
    legitimate subdomains (topics.nytimes.com, search.google.com, podcasts.apple.com,
    starwars.fandom.com, ...) with zero confirmed true positives in testing."""
    provider = BuiltInRulesBlacklistProvider()
    for domain in ["topics.nytimes.com", "search.google.com", "podcasts.apple.com", "starwars.fandom.com"]:
        assert not provider.is_domain_blacklisted(domain), domain


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
