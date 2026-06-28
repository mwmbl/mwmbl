"""Per-site metadata for Super Search source selection.

Joins each registered source with metadata used as bandit features and
cold-start priors: its ``field`` (category), ``domain``, and a numeric
popularity / estimated-pages prior taken from the curated shortlist
(``devdata/super-search-shortlist.json``).

Recipe sources carry ``domain``/``field`` directly (parsed by ``recipe.py``);
the handful of hand-written Python adapters have static metadata here. The
shortlist supplies popularity / page-count priors by domain.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SHORTLIST = _REPO_ROOT / "devdata" / "super-search-shortlist.json"

# Map the shortlist's ordinal levels to a numeric prior in [0, 1].
LEVEL_SCORE = {"high": 1.0, "medium": 0.6, "low": 0.3}
_DEFAULT_SCORE = 0.6


@dataclass(frozen=True)
class SiteMeta:
    name: str                      # source key in SOURCES
    domain: str
    field: str = "other"
    popularity: float = _DEFAULT_SCORE
    estimated_pages: float = _DEFAULT_SCORE
    always_on: bool = False         # global sources always included in selection


# Hand-written adapters: not in the shortlist (or special/global), so metadata is static.
_STATIC_META: dict[str, SiteMeta] = {
    "mwmbl": SiteMeta("mwmbl", "mwmbl.org", "other", 1.0, 1.0, always_on=True),
    "hn": SiteMeta("hn", "news.ycombinator.com", "tech", 1.0, 0.6, always_on=True),
    "github": SiteMeta("github", "github.com", "programming", 1.0, 1.0),
    "stackexchange": SiteMeta("stackexchange", "stackoverflow.com", "programming", 1.0, 1.0),
    "arxiv": SiteMeta("arxiv", "arxiv.org", "academia", 1.0, 1.0),
    "pypi": SiteMeta("pypi", "pypi.org", "programming", 0.6, 0.6),
    "imdb": SiteMeta("imdb", "www.imdb.com", "media", 1.0, 1.0),
}


def _shortlist_path() -> Path:
    return Path(getattr(settings, "SUPER_SEARCH_SHORTLIST_PATH", _DEFAULT_SHORTLIST))


@lru_cache(maxsize=1)
def _load_shortlist() -> dict[str, dict]:
    """Map domain -> shortlist entry. Missing file -> empty (defaults are used)."""
    path = _shortlist_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("Could not load Super Search shortlist %s: %s", path, e)
        return {}
    return {entry["name"]: entry for entry in data.get("domains", [])}


@lru_cache(maxsize=1)
def get_registry() -> dict[str, SiteMeta]:
    """Build metadata for every registered source, keyed by source name.

    Imported lazily to avoid a circular import with ``super_search_sources``.
    """
    from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipes

    registry: dict[str, SiteMeta] = dict(_STATIC_META)
    shortlist = _load_shortlist()
    for name, recipe in load_recipes().items():
        if name in registry:
            continue
        entry = shortlist.get(recipe.domain, {})
        registry[name] = SiteMeta(
            name=name,
            domain=recipe.domain,
            field=recipe.field or "other",
            popularity=LEVEL_SCORE.get(entry.get("popularity"), _DEFAULT_SCORE),
            estimated_pages=LEVEL_SCORE.get(entry.get("estimated_pages"), _DEFAULT_SCORE),
        )
    return registry


def get_meta(name: str) -> SiteMeta:
    """Metadata for ``name``, with a safe default for unknown sources."""
    return get_registry().get(name, SiteMeta(name=name, domain=name))


def all_fields() -> list[str]:
    """Sorted unique field labels across the registry (for one-hot/field features)."""
    return sorted({m.field for m in get_registry().values()})
