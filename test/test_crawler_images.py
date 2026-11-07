from mwmbl.crawler.images import drop_blacklisted_images, image_host_allowed
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider

BLACKLIST = StaticBlacklistProvider({"spam.com"})


def test_image_on_a_blacklisted_host_is_not_allowed():
    assert not image_host_allowed("https://spam.com/a.jpg", BLACKLIST)
    assert image_host_allowed("https://example.com/a.jpg", BLACKLIST)


def test_port_and_credentials_do_not_hide_a_blacklisted_host():
    assert not image_host_allowed("https://spam.com:8443/a.jpg", BLACKLIST)
    assert not image_host_allowed("https://user:pass@SPAM.com/a.jpg", BLACKLIST)


def test_url_without_a_host_is_not_allowed():
    assert not image_host_allowed("https:///a.jpg", BLACKLIST)
    assert not image_host_allowed("http://[bad/a.jpg", BLACKLIST)


def test_drop_blacklisted_images_edits_results_in_place_and_counts():
    results = [
        {"content": {"images": [{"url": "https://spam.com/a.jpg"}, {"url": "https://example.com/b.jpg"}]}},
        {"content": None},
        {"content": {"images": []}},
    ]

    dropped = drop_blacklisted_images(results, BLACKLIST)

    assert dropped == 1
    assert results[0]["content"]["images"] == [{"url": "https://example.com/b.jpg"}]
