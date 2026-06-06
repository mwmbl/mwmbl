"""External-search adapters used by Super Search.

Each adapter exposes ``async def search(client, query, limit) -> list[Document]``
and is responsible for one external API. Adapters never raise on HTTP/parse
errors — they log and return an empty list so one slow source can't sink the
orchestrator.
"""
from mwmbl.tinysearchengine.super_search_sources.arxiv import search as search_arxiv
from mwmbl.tinysearchengine.super_search_sources.github import search as search_github
from mwmbl.tinysearchengine.super_search_sources.hn import search as search_hn
from mwmbl.tinysearchengine.super_search_sources.libraries import search as search_libraries
from mwmbl.tinysearchengine.super_search_sources.mwmbl_index import search as search_mwmbl
from mwmbl.tinysearchengine.super_search_sources.pypi import search as search_pypi
from mwmbl.tinysearchengine.super_search_sources.stackexchange import search as search_stackexchange

SOURCES = {
    "mwmbl": search_mwmbl,
    "hn": search_hn,
    "github": search_github,
    "stackexchange": search_stackexchange,
    "arxiv": search_arxiv,
    "libraries": search_libraries,
    "pypi": search_pypi,
}
