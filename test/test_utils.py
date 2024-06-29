import pytest
from django.core.exceptions import ValidationError

from mwmbl.utils import parse_url, validate_domain


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
