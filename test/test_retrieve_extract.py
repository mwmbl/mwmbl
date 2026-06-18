"""Tests for the extract fallbacks in mwmbl.crawler.retrieve.

justext yields no 'good' content for JS-first pages and for non-English pages
(the English stoplist classifies real prose as boilerplate). crawl_url then
falls back, in order, to Open Graph tags, the meta description, and the first
substantive paragraph. These tests cover the helpers and that fallback chain.
"""
import fakeredis

from mwmbl.crawler import retrieve
from mwmbl.crawler.retrieve import (
    get_first_paragraph,
    get_meta_description,
)
from mwmbl.justext.core import html_to_dom


def _dom(html: str):
    return html_to_dom(html.encode("utf8"), "utf8", None, "replace")


def test_get_meta_description():
    dom = _dom('<html><head>'
               '<meta name="Description" content="  A short summary.  ">'
               '</head><body></body></html>')
    assert get_meta_description(dom) == "A short summary."


def test_get_meta_description_absent():
    assert get_meta_description(_dom("<html><body><p>x</p></body></html>")) == ""


def test_get_first_paragraph_returns_first_substantive():
    dom = _dom("<html><body>"
               "<p>Hi</p>"  # too short, skipped
               "<p>This is the first real paragraph of body content.</p>"
               "<p>A later paragraph.</p>"
               "</body></html>")
    assert get_first_paragraph(dom) == "This is the first real paragraph of body content."


def test_get_first_paragraph_skips_link_heavy():
    dom = _dom("<html><body>"
               '<p><a href="/a">Home</a> <a href="/b">Products</a> <a href="/c">About us</a></p>'
               "<p>The actual descriptive prose for this page lives here.</p>"
               "</body></html>")
    assert get_first_paragraph(dom) == "The actual descriptive prose for this page lives here."


def test_get_first_paragraph_collapses_whitespace_and_nested_tags():
    dom = _dom("<html><body>"
               "<p>From  <b>Proto-Finnic</b>\n  *kic'as.  Cognate with Finnish.</p>"
               "</body></html>")
    assert get_first_paragraph(dom) == "From Proto-Finnic *kic'as. Cognate with Finnish."


def test_get_first_paragraph_none_when_no_body_prose():
    dom = _dom("<html><body><p>Hi</p><div>Not a paragraph element</div></body></html>")
    assert get_first_paragraph(dom) == ""


def _crawl_html(monkeypatch, html: str) -> dict:
    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    monkeypatch.setattr(retrieve, "fetch", lambda url: (200, html.encode("utf8")))
    return retrieve.crawl_url("https://example.test/page", fakeredis.FakeStrictRedis())


def test_crawl_url_falls_back_to_first_paragraph(monkeypatch):
    # No justext 'good' content (single short non-English line) and no meta tags,
    # so the extract must come from the first substantive paragraph.
    html = ("<html><head><title>kitsas</title></head><body>"
            "<p>kitsas (genitive kitsa, partitive kitsast, comparative kitsam)</p>"
            "</body></html>")
    content = _crawl_html(monkeypatch, html)["content"]
    assert content is not None
    assert content["extract"].startswith("kitsas (genitive kitsa")


def test_crawl_url_prefers_meta_description_over_first_paragraph(monkeypatch):
    html = ('<html><head><title>t</title>'
            '<meta name="description" content="The meta summary.">'
            "</head><body><p>Some other first paragraph text here.</p></body></html>")
    content = _crawl_html(monkeypatch, html)["content"]
    assert content["extract"] == "The meta summary."


def test_crawl_url_prefers_og_description(monkeypatch):
    html = ('<html><head><title>t</title>'
            '<meta property="og:description" content="The OG summary.">'
            '<meta name="description" content="The meta summary.">'
            "</head><body><p>Some other first paragraph text here.</p></body></html>")
    content = _crawl_html(monkeypatch, html)["content"]
    assert content["extract"] == "The OG summary."
