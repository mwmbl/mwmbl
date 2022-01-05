from typing import Dict, Any

import s3fs
from pydantic import BaseModel

from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel


class S3FSKwargsModel(BaseModel):
    # Kwargs passed straight to s3fs.S3FileSystem
    # https://s3fs.readthedocs.io/en/latest/api.html#s3fs.core.S3FileSystem

    class Config:
        # Accept any JSON compatible kwargs
        extra = "allow"
        arbitrary_types_allowed = False


class S3FSConnectionModel(BaseConnectionModel):
    s3fs_kwargs: S3FSKwargsModel

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class S3FSConnection(BaseConnection):

    CONN_TYPE = "s3fs_conn"

    CONN_MODEL = S3FSConnectionModel

    def __init__(self, s3fs_kwargs: Dict[str, Any]):
        """Initialize."""
        self.s3fs_kwargs = s3fs_kwargs
        self.fs = s3fs.S3FileSystem(**self.s3fs_kwargs)
