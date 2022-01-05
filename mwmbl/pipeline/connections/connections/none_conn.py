from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel


class NoneConnectionModel(BaseConnectionModel):
    # No config params

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class NoneConnection(BaseConnection):

    CONN_TYPE = "none_conn"

    CONN_MODEL = NoneConnectionModel

    def __init__(self):
        """Initialize."""
        pass
