"""Declarative, YAML-driven search adapters for Super Search.

A *recipe* is a YAML file describing how to search one external site: the query
URL and parameters, the response format, and where to find the URL / title /
extract in the response. A single generic engine (:func:`search_with_recipe`)
executes any recipe and returns ``list[Document]`` with the same signature as
the hand-written adapters in this package, so onboarding a new site becomes
"drop in a YAML file" instead of "write a Python adapter".

Like the hand-written adapters, the engine never raises on HTTP/parse errors —
it logs and returns ``[]`` so one slow or broken source can't sink the
orchestrator.

See ``recipes/*.yaml`` for examples covering JSON APIs (Wiktionary,
archive.org) and HTML scraping (Project Gutenberg).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import quote, urljoin
from xml.etree.ElementTree import Element, ParseError

import httpx
import yaml
from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

RECIPES_DIR = Path(__file__).parent / "recipes"


@dataclass
class Recipe:
    """A parsed search recipe. ``request`` and ``response`` are the raw YAML maps."""
    name: str
    request: dict
    response: dict
    domain: str = ""
    field: str = ""
    smoke: dict | None = None

    @property
    def response_format(self) -> str:
        return self.response.get("format", "json")


def load_recipe(path: Path | str) -> Recipe:
    data = yaml.safe_load(Path(path).read_text())
    return Recipe(
        name=data["name"],
        domain=data["domain"],
        field=data["field"],
        request=data["request"],
        response=data["response"],
        smoke=data["smoke"],
    )


def load_recipes(directory: Path | str = RECIPES_DIR) -> dict[str, Recipe]:
    """Load every ``*.yaml`` recipe in ``directory``, keyed by recipe name.

    A malformed recipe (missing required keys, bad YAML) raises rather than
    being silently skipped, so a broken recipe fails loudly at load time.
    """
    recipes: dict[str, Recipe] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return recipes
    for path in sorted(directory.glob("*.yaml")):
        recipe = load_recipe(path)
        recipes[recipe.name] = recipe
    return recipes


def make_recipe_source(recipe: Recipe) -> Callable[..., Coroutine[Any, Any, list[Document]]]:
    """Wrap a recipe as a ``search(client, query, limit)`` adapter for ``SOURCES``."""
    async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
        return await search_with_recipe(client, recipe, query, limit)
    return search


async def search_with_recipe(
    client: httpx.AsyncClient, recipe: Recipe, query: str, limit: int
) -> list[Document]:
    req = recipe.request
    method = req.get("method", "GET").upper()
    params = _build_params(req.get("params"), query, limit)
    try:
        response = await client.request(method, req["url"], params=params)
        response.raise_for_status()
        fmt = recipe.response_format
        if fmt == "json":
            return _parse_json(response.json(), recipe.response)
        if fmt == "html":
            return _parse_html(response.text, recipe.response)
        if fmt == "xml":
            return _parse_xml(response.text, recipe.response)
        logger.warning("Recipe %s: unknown response format %r", recipe.name, fmt)
        return []
    except (httpx.HTTPError, ValueError, ParseError, DefusedXmlException) as e:
        logger.info("Recipe source %s failed: %s", recipe.name, e)
        return []


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _substitute(value, query: str, limit: int):
    if isinstance(value, str):
        return value.format(query=query, limit=limit)
    if isinstance(value, list):
        return [_substitute(v, query, limit) for v in value]
    return value


def _build_params(params: dict | None, query: str, limit: int) -> dict:
    return {k: _substitute(v, query, limit) for k, v in (params or {}).items()}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _coerce_str(value, strip_html: bool = False) -> str:
    if value is None:
        return ""
    text = str(value)
    if strip_html:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return text.strip()


def _fill_template(template: str, context: dict) -> str:
    """Fill ``template`` from ``context``, URL-quoting each substituted value."""
    safe = {k: quote(str(v), safe="") for k, v in context.items() if v is not None}
    try:
        return template.format(**safe)
    except (KeyError, IndexError):
        return ""


def _make_doc(values: dict, url: str) -> Document | None:
    if not url:
        return None
    return Document(title=values.get("title", ""), url=url, extract=values.get("extract", ""))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _walk(data, path: str):
    """Walk a dotted path (``a.b.c``) through nested dicts; None if absent."""
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _parse_json(payload, spec: dict) -> list[Document]:
    items = _walk(payload, spec.get("results", "")) or []
    fields = spec["fields"]
    strip = set(spec.get("strip_html", []))
    docs: list[Document] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        values = {
            name: _coerce_str(_walk(item, field_spec), name in strip)
            for name, field_spec in fields.items()
            if name != "url" and isinstance(field_spec, str)
        }
        doc = _make_doc(values, _resolve_url_json(fields.get("url"), item, values))
        if doc is not None:
            docs.append(doc)
    return docs


def _resolve_url_json(url_spec, item: dict, values: dict) -> str:
    if url_spec is None:
        return ""
    if isinstance(url_spec, str):
        return _coerce_str(_walk(item, url_spec))
    if isinstance(url_spec, dict) and "template" in url_spec:
        return _fill_template(url_spec["template"], {**item, **values})
    return ""


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _parse_html(html_text: str, spec: dict) -> list[Document]:
    soup = BeautifulSoup(html_text, "html.parser")
    fields = spec["fields"]
    strip = set(spec.get("strip_html", []))
    base_url = spec.get("base_url", "")
    docs: list[Document] = []
    for el in soup.select(spec["results"]):
        values = {
            name: _select_html(el, field_spec, name in strip)
            for name, field_spec in fields.items()
            if name != "url"
        }
        doc = _make_doc(values, _resolve_url_html(fields.get("url"), el, base_url))
        if doc is not None:
            docs.append(doc)
    return docs


def _select_html(el, spec, strip_html: bool = False) -> str:
    if spec is None:
        return ""
    if isinstance(spec, str):
        spec = {"selector": spec}
    target = el.select_one(spec["selector"]) if spec.get("selector") else el
    if target is None:
        return ""
    attr = spec.get("attr")
    value = target.get(attr, "") if attr else target.get_text(" ", strip=True)
    if not isinstance(value, str):
        return ""
    if strip_html and value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return value.strip()


def _resolve_url_html(url_spec, el, base_url: str) -> str:
    if url_spec is None:
        return ""
    href = _select_html(el, url_spec)
    return urljoin(base_url, href) if (href and base_url) else href


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

def _parse_xml(xml_text: str, spec: dict) -> list[Document]:
    root = fromstring(xml_text)
    results_path = spec.get("results", "")
    items = root.findall(results_path) if results_path else [root]
    fields = spec["fields"]
    strip = set(spec.get("strip_html", []))
    docs: list[Document] = []
    for item in items:
        values = {
            name: _select_xml(item, field_spec, name in strip)
            for name, field_spec in fields.items()
            if name != "url"
        }
        doc = _make_doc(values, _resolve_url_xml(fields.get("url"), item, values))
        if doc is not None:
            docs.append(doc)
    return docs


def _select_xml(item: Element, spec, strip_html: bool = False) -> str:
    value = ""
    if isinstance(spec, str):
        child = item.find(spec)
        value = (child.text or "") if child is not None else ""
    elif isinstance(spec, dict):
        attr, selector = spec.get("attr"), spec.get("selector")
        target = item.find(selector) if selector else item
        if target is not None:
            value = target.get(attr, "") if attr else (target.text or "")
    if strip_html and value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return value.strip()


def _resolve_url_xml(url_spec, item: Element, values: dict) -> str:
    if url_spec is None:
        return ""
    if isinstance(url_spec, dict) and "template" in url_spec:
        return _fill_template(url_spec["template"], {**item.attrib, **values})
    return _select_xml(item, url_spec)
