import os
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, Response
from pydantic import BaseModel
from starlette.responses import JSONResponse

LEMMY_URL = os.environ["LEMMY_URL"]


class Register(BaseModel):
    username: str
    email: str
    password: str
    password_verify: str


def create_router() -> APIRouter:
    router = APIRouter(prefix="/user", tags=["user"])

    @router.post("/register")
    def register_user(register: Register) -> Response:
        request = requests.post(urljoin(LEMMY_URL, "api/v3/user/register"), json=register.json())
        print("Request", request)
        # TODO: add in missing fields with null values from here: https://join-lemmy.org/api/classes/Register.html
        return Response(content=request.content, status_code=request.status_code, media_type="text/json")

    return router
