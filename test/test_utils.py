import pytest
from django.core.exceptions import ValidationError

from mwmbl.utils import normalize_domain, parse_url, validate_domain


def test_parse_url():
    url = "https://www.google.com/search?q=python+parse+url+regex#result"
    parsed = parse_url(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.google.com"
    assert parsed.query_string == "?q=python+parse+url+regex"
    assert parsed.fragment == "#result"


def test_validate_domain_valid():
    validate_domain("google.com")
    validate_domain("www.google.com")
    validate_domain("www.google.co.uk")


def test_validate_domain_invalid():
    with pytest.raises(ValidationError):
        validate_domain("google")


def test_validate_url_domain_invalid():
    with pytest.raises(ValidationError):
        validate_domain("https://google/something")


def test_validate_with_url():
    validate_domain("https://www.google.com")
    validate_domain("http://www.google.com")


def test_normalize_domain_from_url():
    assert normalize_domain("https://www.idolbronze.com/") == "www.idolbronze.com"
    assert normalize_domain("http://example.com/some/path?q=1#f") == "example.com"


def test_normalize_domain_leaves_bare_domain_alone():
    assert normalize_domain("example.com") == "example.com"


def test_normalize_domain_strips_path_without_scheme():
    assert normalize_domain("www.example.com/some/path") == "www.example.com"


def test_normalize_domain_strips_query_and_fragment_without_scheme():
    assert normalize_domain("www.example.com?q=1") == "www.example.com"
    assert normalize_domain("www.example.com#fragment") == "www.example.com"


def test_normalize_domain_lowercases():
    assert normalize_domain("https://WWW.Example.COM/") == "www.example.com"
    assert normalize_domain("Example.COM") == "example.com"


def test_validate_domain_rejects_url_without_a_domain():
    with pytest.raises(ValidationError):
        validate_domain("http://")
    with pytest.raises(ValidationError):
        validate_domain("https:///some/path")
