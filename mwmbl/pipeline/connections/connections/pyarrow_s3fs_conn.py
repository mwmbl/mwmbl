from typing import Dict, Any

from pyarrow.fs import S3FileSystem as PAS3FileSystem
from pydantic import BaseModel

from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel


class PyarrowS3FSKwargsModel(BaseModel):
    # Kwargs passed straight to pyarrow.fs.S3FileSystem
    # https://arrow.apache.org/docs/python/generated/pyarrow.fs.S3FileSystem.html#pyarrow.fs.S3FileSystem

    class Config:
        # Accept any JSON compatible kwargs
        extra = "allow"
        arbitrary_types_allowed = False


class PyarrowS3FSConnectionModel(BaseConnectionModel):
    pyarrow_s3fs_kwargs: PyarrowS3FSKwargsModel

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class PyarrowS3FSConnection(BaseConnection):

    CONN_TYPE = "pyarrow_s3fs_conn"

    CONN_MODEL = PyarrowS3FSConnectionModel

    def __init__(self, pyarrow_s3fs_kwargs: Dict[str, Any]):
        """Initialize."""
        self.pyarrow_s3fs_kwargs = pyarrow_s3fs_kwargs
        self.fs = PAS3FileSystem(**self.pyarrow_s3fs_kwargs)
