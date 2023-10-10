from mwmbl.indexer import process_link


def test_process_link_normal():
    url_scores = {"https://somesite.com/something.html": 0.0, "https://somesite.com/": 0.0}
    url_timestamps = {}
    url_users = {}

    process_link(
        user_id_hash="abc123",
        crawled_page_domain="somewhere.com",
        link="https://somesite.com/something.html",
        unknown_domain_multiplier=1,
        timestamp=1234,
        url_scores=url_scores,
        url_timestamps=url_timestamps,
        url_users=url_users,
        is_extra=False, blacklist_domains=[]
    )

    assert url_scores["https://somesite.com/something.html"] > 0.0


def test_process_link_excludes_porn():
    url_scores = {}
    url_timestamps = {}
    url_users = {}

    process_link(
        user_id_hash="abc123",
        crawled_page_domain="somewhere.com",
        link="https://somepornsite.com/something.html",
        unknown_domain_multiplier=1,
        timestamp=1234,
        url_scores=url_scores,
        url_timestamps=url_timestamps,
        url_users=url_users,
        is_extra=False, blacklist_domains=[]
    )

    assert url_scores == {}
    assert url_timestamps == {}
    assert url_users == {}
