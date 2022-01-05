import abc
from typing import Type

from pydantic import BaseModel


class BaseConnectionModel(BaseModel):
    pass


class BaseConnection(abc.ABC):

    CONN_TYPE: str

    CONN_MODEL: Type[BaseConnectionModel]

