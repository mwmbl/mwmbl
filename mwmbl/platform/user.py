import json
import os
from typing import TypeVar, Generic
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, Response
from pydantic import BaseModel

from mwmbl.tokenizer import tokenize

LEMMY_URL = os.environ["LEMMY_URL"]
RESULT_URL = "https://mwmbl.org/?q="


class Register(BaseModel):
    username: str
    email: str
    password: str
    password_verify: str


class Login(BaseModel):
    username_or_email: str
    password: str


class Result(BaseModel):
    url: str
    title: str
    extract: str


class BeginCurate(BaseModel):
    auth: str
    url: str
    results: list[Result]


class CurateMove(BaseModel):
    old_index: int
    new_index: int


class CurateDelete(BaseModel):
    delete_index: int


class CurateAdd(BaseModel):
    insert_index: int
    url: str


class CurateValidate(BaseModel):
    validate_index: int
    is_validated: bool


T = TypeVar('T',  CurateAdd, CurateDelete, CurateMove, CurateValidate)


class Curation(BaseModel, Generic[T]):
    auth: str
    curation_id: int
    curation: T


def create_router() -> APIRouter:
    router = APIRouter(prefix="/user", tags=["user"])

    community_id = get_community_id()

    @router.post("/register")
    def user_register(register: Register) -> Response:
        lemmy_register = {
            "username": register.username,
            "email": register.email,
            "password": register.password,
            "password_verify": register.password_verify,
            "answer": None,
            "captcha_answer": None,
            "captcha_uuid": None,
            "honeypot": None,
            "show_nsfw": False,
        }
        request = requests.post(urljoin(LEMMY_URL, "api/v3/user/register"), json=lemmy_register)
        return Response(content=request.content, status_code=request.status_code, media_type="text/json")

    @router.post("/login")
    def user_login(login: Login) -> Response:
        request = requests.post(urljoin(LEMMY_URL, "api/v3/user/login"), json=login.dict())
        return Response(content=request.content, status_code=request.status_code, media_type="text/json")

    @router.post("/curation/begin")
    def user_begin_curate(begin_curate: BeginCurate):
        results = begin_curate.dict()["results"]
        body = json.dumps({"original_results": results}, indent=2)
        create_post = {
            "auth": begin_curate.auth,
            "body": body,
            "community_id": community_id,
            "honeypot": None,
            "language_id": None,
            "name": begin_curate.url,
            "nsfw": None,
            "url": begin_curate.url,
        }
        request = requests.post(urljoin(LEMMY_URL, "api/v3/post"), json=create_post)
        if request.status_code != 200:
            return Response(content=request.content, status_code=request.status_code, media_type="text/json")
        data = request.json()
        curation_id = data["post_view"]["post"]["id"]
        return {"curation_id": curation_id}

    @router.post("/curation/move")
    def user_move_result(curate_move: Curation[CurateMove]):
        return _create_comment("curate_move", curate_move)

    @router.post("/curation/delete")
    def user_delete_result(curate_delete: Curation[CurateDelete]):
        return _create_comment("curate_delete", curate_delete)

    @router.post("/curation/add")
    def user_add_result(curate_add: Curation[CurateAdd]):
        return _create_comment("curate_add", curate_add)

    @router.post("/curation/validate")
    def user_add_result(curate_validate: Curation[CurateValidate]):
        return _create_comment("curate_validate", curate_validate)

    def _create_comment(curation_type: str, curation: Curation):
        content = json.dumps({curation_type: curation.curation.dict()}, indent=2)
        create_comment = {
            "auth": curation.auth,
            "content": json.dumps(content, indent=2),
            "form_id": None,
            "language_id": None,
            "parent_id": None,
            "post_id": curation.curation_id,
        }
        request = requests.post(urljoin(LEMMY_URL, "api/v3/comment"), json=create_comment)
        return Response(content=request.content, status_code=request.status_code, media_type="text/json")

    return router


def get_community_id() -> str:
    request = requests.get(urljoin(LEMMY_URL, "api/v3/community?name=main"))
    community = request.json()
    return community["community_view"]["community"]["id"]


