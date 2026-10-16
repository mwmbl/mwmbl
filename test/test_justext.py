"""
The vendored jusText: decoding HTML of unknown encoding, and the paragraph classes that
decide what becomes a page's extract.
"""

import pytest

from mwmbl.justext import core
from mwmbl.justext.core import JustextError, decode_html, html_to_dom, justext_from_dom
from mwmbl.justext.utils import get_stoplist

PROSE = (
    "This is a long paragraph of ordinary English prose that talks about the history of the town and "
    "all of the people who have lived in it for the last several hundred years, which is what a reader "
    "of the page would come to it to find out about."
)


def _classes(body: str) -> dict[str, str]:
    dom = html_to_dom(f"<html><body>{body}</body></html>".encode())
    return {p.text: p.class_type for p in justext_from_dom(dom, get_stoplist("English"))}


def test_text_is_returned_unchanged():
    assert decode_html("déjà vu") == "déjà vu"


def test_an_explicit_encoding_wins_over_the_page_declaration():
    html = '<meta charset="utf-8">caf\xe9'.encode("latin-1")

    assert decode_html(html, encoding="latin-1").endswith("café")


def test_the_meta_charset_is_used_when_no_encoding_is_given():
    html = '<meta charset="latin-1">caf\xe9'.encode("latin-1")

    assert decode_html(html).endswith("café")


def test_an_unknown_declared_charset_is_treated_as_undeclared():
    assert decode_html('<meta charset="not-a-codec">café'.encode()).endswith("café")


def test_bytes_that_are_not_utf8_fall_back_to_the_default_encoding():
    assert decode_html(b"caf\xe9", default_encoding="latin-1") == "café"


def test_undecodable_bytes_raise_when_errors_are_strict():
    with pytest.raises(JustextError, match="Unable to decode"):
        decode_html(b"caf\xe9", default_encoding="ascii", errors="strict")


def test_text_carrying_an_xml_encoding_declaration_still_parses():
    dom = html_to_dom('<?xml version="1.0" encoding="utf-8"?><html><body><p>Hello</p></body></html>')

    assert dom.xpath("//p")[0].text == "Hello"


def test_text_input_is_parsed_directly():
    dom = html_to_dom("<html><body><p>Héllo</p></body></html>", encoding="utf-8")

    assert dom.xpath("//p")[0].text == "Héllo"


def test_long_prose_is_good_and_a_copyright_line_is_bad():
    classes = _classes(f"<p>{PROSE}</p><p>\xa9 2024 Example Ltd. All rights reserved.</p>")

    assert classes[PROSE] == "good"
    assert classes["\xa9 2024 Example Ltd. All rights reserved."] == "bad"


def test_a_paragraph_that_is_mostly_links_is_bad():
    link_text = "Read more about the history of the town and the people who lived in it, " * 4
    classes = _classes(f"<p>{PROSE}</p><p><a href='/a'>{link_text}</a></p>")

    assert classes[PROSE] == "good"
    assert classes[link_text.strip()] == "bad"


def test_a_short_linked_label_is_bad_but_a_short_plain_line_between_good_paragraphs_is_good():
    classes = _classes(f"<p>{PROSE}</p><p>Short line.</p><p>{PROSE} Again.</p><p><a href='/x'>Menu</a></p>")

    assert classes["Short line."] == "good"
    assert classes["Menu"] == "bad"


def test_a_short_line_between_boilerplate_is_bad():
    classes = _classes("<p><a href='/a'>Home</a></p><p>Short line.</p><p><a href='/b'>About</a></p>")

    assert classes["Short line."] == "bad"


def test_a_heading_just_before_good_text_is_kept():
    classes = _classes(f"<h2>History</h2><p>{PROSE}</p>")

    assert classes["History"] == "good"


def test_a_heading_far_from_good_text_is_dropped():
    classes = _classes("<h2>History</h2><p><a href='/a'>Home</a></p>" + "<p><a href='/b'>About us now</a></p>" * 20)

    assert classes["History"] == "bad"


def test_br_br_separates_paragraphs():
    dom = html_to_dom("<html><body><p>First line.<br><br>Second line.</p></body></html>".encode())

    paragraphs = justext_from_dom(dom, get_stoplist("English"))

    assert [p.text for p in paragraphs] == ["First line.", "Second line."]


def test_ignored_swallows_only_the_listed_exceptions():
    with core.ignored(LookupError):
        raise LookupError

    with pytest.raises(ValueError):
        with core.ignored(LookupError):
            raise ValueError
