"""Image collection in mwmbl.crawler.retrieve: which <img> elements the crawler reports and the text it keeps."""

import fakeredis

from mwmbl.crawler import retrieve
from mwmbl.crawler.batch import ItemContent
from mwmbl.crawler.retrieve import MAX_IMAGES, get_images
from mwmbl.justext.core import html_to_dom

PAGE = "https://example.test/animals/gorilla"


def _images(body: str) -> list[dict]:
    dom = html_to_dom(f"<html><body>{body}</body></html>".encode(), "utf8", None, "replace")
    return get_images(dom, PAGE)


def test_alt_title_and_figcaption_are_collected_and_the_url_resolved():
    images = _images(
        '<img src="/a.jpg" alt="A  silverback\n gorilla" title="Gorilla" width="400" height="300">'
        '<figure><img src="b.jpg"><figcaption>Feeding <b>time</b></figcaption></figure>'
    )

    assert images == [
        {
            "url": "https://example.test/a.jpg",
            "alt": "A silverback gorilla",
            "title": "Gorilla",
            "caption": "",
            "width": 400,
            "height": 300,
        },
        {
            "url": "https://example.test/animals/b.jpg",
            "alt": "",
            "title": "",
            "caption": "Feeding time",
            "width": None,
            "height": None,
        },
    ]


def test_images_without_text_are_skipped():
    assert _images('<img src="/a.jpg"><img src="/b.jpg" alt="  ">') == []


def test_images_declared_small_are_skipped_but_percentages_are_not():
    images = _images(
        '<img src="/pixel.gif" alt="pixel" width="1" height="1">'
        '<img src="/icon.png" alt="icon" height="16">'
        '<img src="/wide.jpg" alt="wide" width="100%">'
    )

    assert [image["url"] for image in images] == ["https://example.test/wide.jpg"]
    assert images[0]["width"] is None


def test_lazy_loaded_images_use_data_src_and_other_schemes_are_dropped():
    images = _images(
        '<img src="data:image/gif;base64,R0lGOD" data-src="/lazy.jpg" alt="lazy">'
        '<img src="data:image/gif;base64,R0lGOD" alt="inline only">'
        '<img src="ftp://example.test/a.jpg" alt="ftp">'
    )

    assert [image["url"] for image in images] == ["https://example.test/lazy.jpg"]


def test_duplicate_and_overlong_urls_are_dropped():
    images = _images(
        f'<img src="/a.jpg" alt="one"><img src="/a.jpg" alt="again"><img src="/{"x" * 300}.jpg" alt="long">'
    )

    assert [image["url"] for image in images] == ["https://example.test/a.jpg"]


def test_alt_text_is_truncated():
    images = _images(f'<img src="/a.jpg" alt="{"word " * 100}">')

    assert len(images[0]["alt"]) == retrieve.MAX_IMAGE_TEXT_CHARS
    assert images[0]["alt"].endswith("…")


def test_image_count_is_capped():
    body = "".join(f'<img src="/{n}.jpg" alt="image {n}">' for n in range(MAX_IMAGES + 5))

    assert len(_images(body)) == MAX_IMAGES


def test_crawl_url_reports_images_and_the_batch_schema_keeps_them(monkeypatch):
    html = (
        "<html><head><title>Gorillas</title></head><body>"
        "<p>Gorillas are large apes that live in the forests of central Africa and eat plants.</p>"
        '<img src="/g.jpg" alt="A gorilla">'
        "</body></html>"
    )
    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    monkeypatch.setattr(retrieve, "fetch", lambda url: (200, html.encode(), url))

    content = retrieve.crawl_url(PAGE, fakeredis.FakeStrictRedis())["content"]

    assert content["images"][0]["url"] == "https://example.test/g.jpg"
    parsed = ItemContent.model_validate(content)
    assert parsed.images[0].alt == "A gorilla"
