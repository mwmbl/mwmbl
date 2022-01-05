from typing import Any


class StdData(object):
    """An instance of this object is passed around in the pipeline.

    - It can also be thought of as a standard message.
    """

    def __init__(self, data: Any):
        """Initialize the StdData."""
        self.data = data
