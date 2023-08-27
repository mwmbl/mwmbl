import json
import os
import uuid
from typing import TypeVar, Generic, AsyncGenerator, Optional
from urllib.parse import urljoin, parse_qs

import requests
from fastapi import APIRouter, Response, Depends, Request
from fastapi_users import UUIDIDMixin, BaseUserManager
from fastapi_users.authentication import CookieTransport, AuthenticationBackend
from fastapi_users.authentication.strategy import AccessTokenDatabase, DatabaseStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID, SQLAlchemyAccessTokenDatabase
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from mwmbl.settings import DATABASE_URL
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, DocumentState
from mwmbl.tokenizer import tokenize


LEMMY_URL = os.environ["LEMMY_URL"]
RESULT_URL = "https://mwmbl.org/?q="
MAX_CURATED_SCORE = 1_111_111.0


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    pass


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    pass


engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
cookie_transport = CookieTransport(cookie_max_age=3600)


async def create_db_and_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)


async def get_access_token_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyAccessTokenDatabase(session, AccessToken)


def get_database_strategy(
    access_token_db: AccessTokenDatabase[AccessToken] = Depends(get_access_token_db),
) -> DatabaseStrategy:
    return DatabaseStrategy(access_token_db, lifetime_seconds=3600)


auth_backend = AuthenticationBackend(
    name="db",
    transport=cookie_transport,
    get_strategy=get_database_strategy,
)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = ""    # TODO
    verification_token_secret = ""    # TODO

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} has registered.")

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        print(f"User {user.id} has forgot their password. Reset token: {token}")

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        print(f"Verification requested for user {user.id}. Verification token: {token}")


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)
    

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
    curated: bool


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
    url: str
    results: list[Result]
    curation: T


def create_router(index_path: str) -> APIRouter:
    router = APIRouter(prefix="/user", tags=["user"])

    # TODO: reinstate
    # community_id = get_community_id()
    community_id = 0

    @router.post("/register")
    def user_register(register: Register) -> Response:
        lemmy_register = {
            "username": register.username,
            "email": register.email,
            "password": register.password,
            "password_verify": register.password_verify,
            "answer": "not applicable",
            "captcha_answer": None,
            "captcha_uuid": None,
            "honeypot": None,
            "show_nsfw": False,
        }
        request = requests.post(urljoin(LEMMY_URL, "api/v3/user/register"), json=lemmy_register)
        if request.status_code != 200:
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
        return _curate("curate_move", curate_move)

    @router.post("/curation/delete")
    def user_delete_result(curate_delete: Curation[CurateDelete]):
        return _curate("curate_delete", curate_delete)

    @router.post("/curation/add")
    def user_add_result(curate_add: Curation[CurateAdd]):
        return _curate("curate_add", curate_add)

    @router.post("/curation/validate")
    def user_add_result(curate_validate: Curation[CurateValidate]):
        return _curate("curate_validate", curate_validate)

    def _curate(curation_type: str, curation: Curation):
        content = json.dumps({
            "curation_type": curation_type,
            "curation": curation.curation.dict(),
        }, indent=2)
        create_comment = {
            "auth": curation.auth,
            "content": json.dumps(content, indent=2),
            "form_id": None,
            "language_id": None,
            "parent_id": None,
            "post_id": curation.curation_id,
        }
        request = requests.post(urljoin(LEMMY_URL, "api/v3/comment"), json=create_comment)

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

        return Response(content=request.content, status_code=request.status_code, media_type="text/json")

    return router


def get_community_id() -> str:
    request = requests.get(urljoin(LEMMY_URL, "api/v3/community?name=main"))
    community = request.json()
    return community["community_view"]["community"]["id"]


