"""External-search adapters used by Super Search.

Each adapter exposes ``async def search(client, query, limit) -> list[Document]``
and is responsible for one external API. Adapters never raise on HTTP/parse
errors — they log and return an empty list so one slow source can't sink the
orchestrator.
"""
from mwmbl.tinysearchengine.super_search_sources.arxiv import search as search_arxiv
from mwmbl.tinysearchengine.super_search_sources.github import search as search_github
from mwmbl.tinysearchengine.super_search_sources.hn import search as search_hn
from mwmbl.tinysearchengine.super_search_sources.mwmbl_index import search as search_mwmbl
from mwmbl.tinysearchengine.super_search_sources.pypi import search as search_pypi
from mwmbl.tinysearchengine.super_search_sources.recipe import load_recipes, make_recipe_source
from mwmbl.tinysearchengine.super_search_sources.stackexchange import search as search_stackexchange

SOURCES = {
    "mwmbl": search_mwmbl,
    "hn": search_hn,
    "github": search_github,
    "stackexchange": search_stackexchange,
    "arxiv": search_arxiv,
    "pypi": search_pypi,
}

# Declarative YAML recipes (recipes/*.yaml) are loaded at import time and added
# as ordinary sources. A hand-written adapter wins if it shares a recipe's name.
for _name, _recipe in load_recipes().items():
    SOURCES.setdefault(_name, make_recipe_source(_recipe))
