from ninja import NinjaAPI
from ninja.security import django_auth

import mwmbl.crawler.app as crawler
from mwmbl.platform import curate
from mwmbl.search_setup import queued_batches, index_path, ranker, batch_cache
from mwmbl.tinysearchengine import search


def create_api(version):
    # Set csrf to True to all cookie-based authentication
    api = NinjaAPI(version=version, csrf=True)

    search_router = search.create_router(ranker)
    api.add_router("/search/", search_router)

    crawler_router = crawler.create_router(batch_cache=batch_cache, queued_batches=queued_batches)
    api.add_router("/crawler/", crawler_router)

    curation_router = curate.create_router(index_path)
    api.add_router("/curation/", curation_router, auth=django_auth)
    return api


# Work around because Django-Ninja doesn't allow using multiple URLs for the same thing
api_original = create_api("0.1")
api_v1 = create_api("1.0.0")
