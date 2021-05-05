"""
Filesystem-based queue that uses os.rename as an atomic operation to ensure
that items are handled correctly.
"""

import json
import os
from abc import ABC
from enum import Enum
from uuid import uuid4
from pathlib import Path

from zstandard import ZstdCompressor, ZstdDecompressor


class FSState(Enum):
    CREATING = 'creating'
    READY = 'ready'
    LOCKED = 'locked'
    DONE = 'done'


class Serializer(ABC):
    def serialize(self, item) -> bytes:
        pass

    def deserialize(self, serialized_item: bytes):
        pass


class ZstdJsonSerializer(Serializer):
    def __init__(self):
        self.compressor = ZstdCompressor()
        self.decompressor = ZstdDecompressor()

    def serialize(self, item) -> bytes:
        return self.compressor.compress(json.dumps(item).encode('utf8'))

    def deserialize(self, serialized_item: bytes):
        return json.loads(self.decompressor.decompress(serialized_item).decode('utf8'))


class FSQueue:
    def __init__(self, directory: str, name: str, serializer: Serializer):
        self.directory = directory
        self.name = name
        self.serializer = serializer

        if not os.path.isdir(self.directory):
            raise ValueError("Given path is not a directory")

        if '/' in name:
            raise ValueError("Name should not contain '/'")

        os.makedirs(os.path.join(self.directory, self.name), exist_ok=True)
        for state in FSState:
            os.makedirs(self._get_dir(state), exist_ok=True)

    def _get_dir(self, state: FSState):
        return os.path.join(self.directory, self.name, state.value)

    def _get_path(self, state: FSState, name: str):
        return os.path.join(self._get_dir(state), name)

    def _move(self, name: str, old_state: FSState, new_state: FSState):
        os.rename(self._get_path(old_state, name), self._get_path(new_state, name))

    def put(self, item: object):
        """
        Push a new item into the ready state
        """
        item_id = str(uuid4())
        with open(self._get_path(FSState.CREATING, item_id), 'wb') as output_file:
            output_file.write(self.serializer.serialize(item))

        self._move(item_id, FSState.CREATING, FSState.READY)

    def get(self) -> (str, object):
        """
        Get the next priority item from the queue, returning the item ID and the object
        """

        paths = sorted(Path(self._get_dir(FSState.READY)).iterdir(), key=os.path.getmtime)

        for path in paths:
            # Try and lock the file
            self._move(path.name, FSState.READY, FSState.LOCKED)

            with open(self._get_path(FSState.LOCKED, path.name), 'rb') as item_file:
                return path.name, self.serializer.deserialize(item_file.read())

    def done(self, item_id: str):
        """
        Mark a task/file as done
        """

        self._move(item_id, FSState.LOCKED, FSState.DONE)
