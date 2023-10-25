import json
from urllib.parse import urljoin, parse_qs

import requests
from ninja import Router
from ninja.security import django_auth

from mwmbl.indexer.update_urls import get_datetime_from_timestamp
from mwmbl.models import UserCuration
from mwmbl.platform.data import CurateBegin, CurateMove, CurateDelete, CurateAdd, CurateValidate, Curation
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tokenizer import tokenize


RESULT_URL = "https://mwmbl.org/?q="
MAX_CURATED_SCORE = 1_111_111.0


def create_router(index_path: str) -> Router:
    router = Router(tags=["user"])

    @router.post("/begin", auth=django_auth)
    def user_begin_curate(request, curate_begin: CurateBegin):
        return _curate(request, "curate_begin", curate_begin)

    @router.post("/move", auth=django_auth)
    def user_move_result(request, curate_move: Curation[CurateMove]):
        return _curate(request, "curate_move", curate_move)

    @router.post("/delete", auth=django_auth)
    def user_delete_result(request, curate_delete: Curation[CurateDelete]):
        return _curate(request, "curate_delete", curate_delete)

    @router.post("/add", auth=django_auth)
    def user_add_result(request, curate_add: Curation[CurateAdd]):
        return _curate(request, "curate_add", curate_add)

    @router.post("/validate", auth=django_auth)
    def user_add_result(request, curate_validate: Curation[CurateValidate]):
        return _curate(request, "curate_validate", curate_validate)

    def _curate(request, curation_type: str, curation: Curation):
        user_curation = UserCuration(
            user=request.user,
            timestamp=get_datetime_from_timestamp(curation.timestamp),
            url=curation.url,
            results=curation.dict()["results"],
            curation_type=curation_type,
            curation=curation.curation.dict(),
        )
        user_curation.save()

        with TinyIndex(Document, index_path, 'w') as indexer:
            query_string = parse_qs(curation.url)
            if len(query_string) > 1:
                raise ValueError(f"Should be one query string in the URL: {curation.url}")

            queries = next(iter(query_string.values()))
            if len(queries) > 1:
                raise ValueError(f"Should be one query value in the URL: {curation.url}")

            query = queries[0]
            print("Query", query)
            tokens = tokenize(query)
            print("Tokens", tokens)
            term = " ".join(tokens)
            print("Key", term)

            documents = [
                Document(result.title, result.url, result.extract, MAX_CURATED_SCORE - i, term, result.curated)
                for i, result in enumerate(curation.results)
            ]
            page_index = indexer.get_key_page_index(term)
            print("Page index", page_index)
            print("Storing documents", documents)
            indexer.store_in_page(page_index, documents)

        return {"curation": "ok"}

    return router


