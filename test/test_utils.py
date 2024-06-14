from mwmbl.utils import parse_url


def test_parse_url():
    url = "https://www.google.com/search?q=python+parse+url+regex#result"
    parsed = parse_url(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.google.com"
    assert parsed.query_string == "?q=python+parse+url+regex"
    assert parsed.fragment == "#result"
