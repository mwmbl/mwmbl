"""
LLM relabel — Pass 1 prep: build the source catalog Haiku chooses from, and
sample queries for query-analysis (intent + source selection).

This only PREPARES inputs (catalog + sampled queries) and writes the reusable
catalog to devdata/llm_relabel/source_catalog.json. The actual Haiku judging is
driven separately (batched), then checkpointed.

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        uv run python scripts/llm_relabel_pass1.py --num 100 --seed 42
"""
import json
import os
from argparse import ArgumentParser

import django
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH  # noqa: E402
from mwmbl.tinysearchengine.super_search_sources import SOURCES  # noqa: E402
from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipes  # noqa: E402

CATALOG_PATH = "devdata/llm_relabel/source_catalog.json"

# Hand descriptions for the non-recipe adapters.
ADAPTER_DESC = {
    "mwmbl": ("general", "Mwmbl's own crawled web index — general web pages on any topic."),
    "hn": ("tech", "Hacker News — tech, startups, programming, and science links/discussion."),
    "github": ("code", "GitHub — source-code repositories and software projects."),
    "stackexchange": ("qa", "Stack Exchange — Q&A across programming and many technical topics."),
    "arxiv": ("academia", "arXiv — academic preprints in physics, CS, maths, biology, etc."),
    "pypi": ("code", "PyPI — the Python package index."),
    "imdb": ("entertainment", "IMDb — films, TV shows, actors, and entertainment."),
    "nhs": ("health", "NHS — authoritative UK consumer health: conditions, symptoms, treatments, medicines and dosages."),
    "openstreetmap_org": ("maps-places", "OpenStreetMap — places, towns, addresses, and local points of interest (maps/local)."),
    "wikidata_official": ("navigational", "Official website of a named brand, company, organisation or person (resolved via Wikidata)."),
    "homepage": ("navigational", "Best-guess official homepage for a brand or site name — for navigational brand queries."),
    "guardian": ("news", "The Guardian — UK/world news, politics, sport and current events."),
}

# Post-hoc intent->source augmentation. Haiku classifies intent reliably but
# under-routes the navigational/local/news adapters, so rather than force its
# hand we add the obvious sources deterministically once it tags the intent.
INTENT_SOURCES = {
    "navigational": ["wikidata_official", "homepage"],
    "local": ["openstreetmap_org"],
    "news": ["guardian"],
}


def augment_sources(intent: str, sources: list[str]) -> list[str]:
    """Layer deterministic intent-driven sources onto Haiku's picks (dedup, keep order)."""
    out = list(dict.fromkeys([*sources, *INTENT_SOURCES.get(intent, [])]))
    if "mwmbl" not in out:
        out.append("mwmbl")  # always keep the general index as a baseline
    return out


def _targets_lookup() -> dict:
    """domain -> reason, from super-search-targets.json (keyed raw and www-stripped)."""
    out = {}
    try:
        domains = json.load(open("super-search-targets.json"))["domains"]
    except Exception:
        return out
    for d in domains:
        name = d.get("name", "")
        reason = d.get("reason") or ""
        field = d.get("field") or ""
        for key in {name, name[4:] if name.startswith("www.") else name, "www." + name}:
            out[key] = {"reason": reason, "field": field}
    return out


def build_catalog() -> dict:
    targets = _targets_lookup()
    recipes = load_recipes()
    catalog = {}
    for name in SOURCES:
        if name in ADAPTER_DESC:
            field, desc = ADAPTER_DESC[name]
            catalog[name] = {"field": field, "description": desc}
            continue
        rec = recipes.get(name)
        domain = getattr(rec, "domain", name) if rec else name
        field = getattr(rec, "field", "") if rec else ""
        t = targets.get(domain) or {}
        desc = t.get("reason") or f"{field or 'web'} site ({domain})"
        catalog[name] = {"field": t.get("field") or field, "description": desc, "domain": domain}
    return catalog


def main():
    parser = ArgumentParser()
    parser.add_argument("--num", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    catalog = build_catalog()
    with open(CATALOG_PATH, "w") as f:
        json.dump(catalog, f, indent=2)
    print(f"Wrote {CATALOG_PATH} ({len(catalog)} sources)")

    queries = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH)["query"].unique()
    rng = np.random.default_rng(args.seed)
    sample = sorted(rng.choice(queries, args.num, replace=False).tolist())

    # Catalog text block (for the Haiku prompt)
    print("\n===CATALOG===")
    for name, meta in sorted(catalog.items()):
        print(f"{name} [{meta.get('field','')}]: {meta['description']}")
    print("\n===QUERIES===")
    for q in sample:
        print(q)


if __name__ == "__main__":
    main()
