from __future__ import annotations

from typing import Optional, List, Dict, Any

from pydantic import BaseModel

from mwmbl.pipeline.connections.connection_catalog import AnyConnection
from mwmbl.pipeline.connections.connection_handler import ConnectionHandler, ConnectionHandlerModel


class ConnectionGroupHandlerModel(BaseModel):
    conn_group_config: Optional[List[ConnectionHandlerModel]] = None


class ConnectionGroupHandler(object):

    def __init__(
            self,
            conn_group_config: Optional[List[Dict[str, Any]]] = None,
    ):
        """Initialize ConnectionGroupHandler.

        - Initialize the individual Connections via ConnectionHandlers
        - ConnectionHandlers will take care of validating the config for each Op
        """
        self.conn_group_config = conn_group_config if conn_group_config else []
        self.conn_handlers = [
            ConnectionHandler(**conn_config) for conn_config in self.conn_group_config
        ]
        self.conn_handlers_map: Dict[str, AnyConnection] = {
            conn_handler.conn_name: conn_handler.conn for conn_handler in self.conn_handlers
        }

        # Initialize GLOBAL_CONNECTIONS_HANDLER with this instance of ConnectionGroupHandler
        global GLOBAL_CONNECTIONS_HANDLER
        GLOBAL_CONNECTIONS_HANDLER = self

        print(f"ConnectionGroupHandler: Initialized all connections")

    def get_conn(self, conn_name: str) -> AnyConnection:
        """Get connection from unique conn_name."""
        return self.conn_handlers_map[conn_name]


GLOBAL_CONNECTIONS_HANDLER: Optional[ConnectionGroupHandler] = None


def init_global_connections_handler(conn_group_config) -> ConnectionGroupHandler:
    global GLOBAL_CONNECTIONS_HANDLER

    if GLOBAL_CONNECTIONS_HANDLER is None:
        GLOBAL_CONNECTIONS_HANDLER = ConnectionGroupHandler(conn_group_config=conn_group_config)
    else:
        print("GLOBAL_CONNECTIONS_HANDLER has already been initialized.")

    return GLOBAL_CONNECTIONS_HANDLER


def get_global_connections_handler() -> ConnectionGroupHandler:
    global GLOBAL_CONNECTIONS_HANDLER

    if GLOBAL_CONNECTIONS_HANDLER is None:
        raise ValueError(f"GLOBAL_CONNECTIONS_HANDLER has not been initialized.")
    else:
        return GLOBAL_CONNECTIONS_HANDLER


if __name__ == "__main__":
    config = {
        "conn_group_config": [
            {
                "conn_name": "dummy",
                "conn_type": "none_conn",
                "conn_config": {}
            }
        ]
    }
    # Validate config
    ConnectionGroupHandlerModel(**config)

    # Initialize ConnectionGroupHandler
    conn_group_handler = ConnectionGroupHandler(**config)
    conn_group_handler.get_conn(conn_name="dummy")

    # Use GLOBAL_CONNECTIONS_HANDLER
    global_connections_handler = get_global_connections_handler()
    global_connections_handler.get_conn(conn_name="dummy")

    print("Done")


