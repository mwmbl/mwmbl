from typing import Dict, Any

from pydantic import BaseModel, root_validator, StrictStr

from mwmbl.pipeline.connections.connection_catalog import CONN_CATALOG
from mwmbl.pipeline.connections.connections.base import BaseConnection


class ConnectionHandlerModel(BaseModel):
    conn_name: StrictStr
    conn_type: StrictStr
    # Accepts emtpy dict
    conn_config: Dict[StrictStr, Any]

    @root_validator(pre=False)
    def validate_conn_type_conn_config(cls, values):
        """Validate conn_type & conn_config.

        - Make sure that conn_type exists in the CONN_CATALOG
        - Make sure that conn_config is a valid config by initializing the OpModel for the
          corresponding Op class based on conn_type.
        """
        conn_type = values.get("conn_type", None)
        conn_config = values.get("conn_config", None)

        if conn_type not in CONN_CATALOG.keys():
            raise ValueError(f"CONN_TYPE: {conn_type} does not exist in CONN_CATALOG.")

        conn_class = CONN_CATALOG[conn_type]
        conn_model = conn_class.CONN_MODEL

        # Validate the conn_config by just initializing the OpModel
        conn_model(**conn_config)

        return values

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class ConnectionHandler(object):

    def __init__(
            self,
            conn_name: str,
            conn_type: str,
            conn_config: Dict[str, Any],
    ):
        """."""
        self.conn_name = conn_name
        if conn_type not in CONN_CATALOG:
            raise ValueError(
                f"CONN_TYPE: '{conn_type}' not found in CONN_CATALOG. Corresponding conn_name: '{conn_name}'"
            )

        self.conn_type = conn_type
        self.conn_config = conn_config

        self.conn_class = CONN_CATALOG[self.conn_type]
        self.conn: BaseConnection = self.conn_class(**self.conn_config)

        print(f"ConnectionHandler: Initialized. conn_name: {self.conn_name}, conn_type: {self.conn_type}")



if __name__ == "__main__":
    config = {
        "conn_name": "dummy",
        "conn_type": "none_conn",
        "conn_config": {}
    }
    # TODO: Why does Pycharm show the error "'null' extra fields not permitted". But it works.
    # validate config
    ConnectionHandlerModel(**config)
    # Init ConnectionHandler
    ConnectionHandler(**config)
    print("Done")
