"""Per-source adapter tests for Super Search.

Each adapter is exercised against a mocked httpx transport so we can verify
result coercion without hitting the network. We also assert that an HTTP
error returns an empty list rather than propagating — orchestrator robustness
depends on this.
"""
import re

import httpx
import pytest

from mwmbl.tinysearchengine.super_search_sources.arxiv import search as search_arxiv
from mwmbl.tinysearchengine.super_search_sources.github import search as search_github
from mwmbl.tinysearchengine.super_search_sources.hn import search as search_hn
from mwmbl.tinysearchengine.super_search_sources.pypi import search as search_pypi
from mwmbl.tinysearchengine.super_search_sources.stackexchange import search as search_stackexchange


async def _client():
    return httpx.AsyncClient()


# ---------------------------------------------------------------------------
# Hacker News (Algolia)
# ---------------------------------------------------------------------------

async def test_hn_returns_documents(httpx_mock):
    httpx_mock.add_response(
        url="https://hn.algolia.com/api/v1/search?query=python&hitsPerPage=5&tags=story",
        json={"hits": [
            {"title": "Why Python", "url": "https://example.com/why", "objectID": "1"},
            {"title": "Ask HN", "story_text": "Some text", "objectID": "2"},  # no url
        ]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_hn(client, "python", 5)
    assert len(docs) == 2
    assert docs[0].url == "https://example.com/why"
    assert docs[1].url.startswith("https://news.ycombinator.com/item?id=")


async def test_hn_swallows_http_errors(httpx_mock):
    httpx_mock.add_response(status_code=500)
    async with httpx.AsyncClient() as client:
        docs = await search_hn(client, "python", 5)
    assert docs == []


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

async def test_github_returns_documents(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://api\.github\.com/search/repositories.*"),
        json={"items": [
            {"full_name": "psf/requests", "html_url": "https://github.com/psf/requests",
             "description": "HTTP for humans"},
            {"name": "noop", "html_url": ""},  # missing url -> skipped
        ]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_github(client, "requests", 5)
    assert len(docs) == 1
    assert docs[0].title == "psf/requests"


async def test_github_swallows_errors(httpx_mock):
    httpx_mock.add_response(status_code=403)
    async with httpx.AsyncClient() as client:
        docs = await search_github(client, "anything", 5)
    assert docs == []


# ---------------------------------------------------------------------------
# Stack Exchange
# ---------------------------------------------------------------------------

async def test_stackexchange_returns_documents(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://api\.stackexchange\.com/.*"),
        json={"items": [
            {"title": "How do I &amp; X?", "link": "https://stackoverflow.com/q/1",
             "tags": ["python", "async"], "score": 12},
        ]},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_stackexchange(client, "async python", 5)
    assert len(docs) == 1
    # HTML entities are unescaped
    assert "&" in docs[0].title and "amp" not in docs[0].title
    assert "python" in docs[0].extract
    assert "12" in docs[0].extract


# ---------------------------------------------------------------------------
# ArXiv
# ---------------------------------------------------------------------------

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>Attention Is All You Need</title>
    <summary>We propose...</summary>
  </entry>
</feed>
"""

async def test_arxiv_returns_documents(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://export\.arxiv\.org/.*"),
        text=ARXIV_ATOM,
    )
    async with httpx.AsyncClient() as client:
        docs = await search_arxiv(client, "attention", 5)
    assert len(docs) == 1
    assert docs[0].url == "http://arxiv.org/abs/2401.00001v1"
    assert "Attention" in docs[0].title


# ---------------------------------------------------------------------------
# PyPI
# ---------------------------------------------------------------------------

async def test_pypi_returns_document(httpx_mock):
    httpx_mock.add_response(
        url="https://pypi.org/pypi/requests/json",
        json={"info": {"name": "requests", "summary": "HTTP for humans",
                       "package_url": "https://pypi.org/project/requests/"}},
    )
    async with httpx.AsyncClient() as client:
        docs = await search_pypi(client, "requests", 5)
    assert len(docs) == 1
    assert docs[0].title == "requests"
    assert "HTTP" in docs[0].extract


async def test_pypi_404_returns_empty(httpx_mock):
    httpx_mock.add_response(status_code=404)
    async with httpx.AsyncClient() as client:
        docs = await search_pypi(client, "nonsense", 5)
    assert docs == []


async def test_pypi_rejects_invalid_name():
    async with httpx.AsyncClient() as client:
        # Spaces / punctuation -> not a valid package name
        docs = await search_pypi(client, "this is a phrase!", 5)
    assert docs == []
