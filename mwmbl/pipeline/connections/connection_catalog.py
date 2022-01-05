"""Manually managed & curated Connection types.

1. First import a new Connection that you have implemented.
2. Add the Connection class to the CONN_CLASSES list.
"""
from collections import defaultdict
from typing import List, Dict, Union, Type

from mwmbl.pipeline.connections.connections.base import BaseConnection
from mwmbl.pipeline.connections.connections.none_conn import NoneConnection
from mwmbl.pipeline.connections.connections.pyarrow_s3fs_conn import PyarrowS3FSConnection
from mwmbl.pipeline.connections.connections.s3fs_conn import S3FSConnection

# --------------------------------------------------------------------------------------------------
# Add your implemented Connection class to the CONN_CLASSES list
# --------------------------------------------------------------------------------------------------
# The list can contain any class that is a subclass of BaseConnection
CONN_CLASSES: List[Type[BaseConnection]] = [
    NoneConnection,
    S3FSConnection,
    PyarrowS3FSConnection,
]

AnyConnection = Union[
    NoneConnection,
    S3FSConnection,
    PyarrowS3FSConnection,
]
# --------------------------------------------------------------------------------------------------

def _validate_no_duplicate_conn_types(conn_classes):
    """Validate that there are no duplicate conn_types.

    This check is expected to run at runtime, every time the module is loaded for the first time.
    """
    # If a key doesn't exist in the dict, it is initialized with an empty list.
    conn_type_to_class_map = defaultdict(list)

    for conn_class in conn_classes:
        conn_type_to_class_map[conn_class.CONN_TYPE].append(conn_class)

    # str(v) -> returns the __repr__ of the Connection class.
    conn_type_count_offenders = {k: str(v) for k, v in conn_type_to_class_map.items() if len(v) > 1}

    if conn_type_count_offenders:
        offenders = [
            f"* CONN_TYPE: '{conn_type}' is duplicated for these CONN_CLASSES: {conn_classes}"
            for conn_type, conn_classes in conn_type_count_offenders.items()
        ]
        offenders_str = "\n".join(offenders)

        # This is an important validation that gets checked at runtime.
        # A verbose error message is justified.
        raise ValueError(
            f"The CONN_CATALOG cannot be contructed since there are some Connections that have duplicated "
            f"`conn_types`. Please make sure that the `conn_types` are unique across all Connections. The "
            f"following is the list duplicate `conn_types` and their corresponding Connection classes:\n"
            f"{offenders_str}"
        )


_validate_no_duplicate_conn_types(CONN_CLASSES)

# Finally, construct the CONN_CATALOG which is a mapping from Connection.CONN_TYPE -> Connection
# Given an `conn_type`, the CONN_CATALOG will tell you the corresponding Connection class.
CONN_CATALOG: Dict[str, Type[BaseConnection]] = {
    conn_class.CONN_TYPE: conn_class for conn_class in CONN_CLASSES
}


if __name__ == "__main__":
    # Call using `python -m mwmbl.pipeline.connections.connection_catalog`
    print("Done")
