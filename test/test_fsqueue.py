"""
The filesystem queue moves each item between state directories with os.rename, so an item
is only ever in one state and two consumers can never take the same one.
"""

from pathlib import Path

import pytest

from mwmbl.indexer.fsqueue import (
    FSQueue,
    FSQueueError,
    FSState,
    GzipJsonBlobSerializer,
    GzipJsonRowSerializer,
    ZstdJsonSerializer,
)


@pytest.fixture
def queue(tmp_path):
    return FSQueue(tmp_path, "items", GzipJsonBlobSerializer())


def _names(queue: FSQueue, state: FSState) -> list[str]:
    return sorted(path.name for path in Path(queue._get_dir(state)).iterdir())


@pytest.mark.parametrize(
    "serializer, item",
    [
        (ZstdJsonSerializer(), {"a": [1, 2, 3]}),
        (GzipJsonBlobSerializer(), {"a": [1, 2, 3]}),
        (GzipJsonRowSerializer(), [{"a": 1}, {"b": 2}]),
    ],
)
def test_serializers_round_trip(serializer, item):
    assert serializer.deserialize(serializer.serialize(item)) == item


def test_queue_creates_a_directory_per_state(queue):
    for state in FSState:
        assert _names(queue, state) == []


def test_queue_requires_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        FSQueue(tmp_path / "missing", "items", GzipJsonBlobSerializer())


def test_queue_name_cannot_contain_a_slash(tmp_path):
    with pytest.raises(ValueError, match="'/'"):
        FSQueue(tmp_path, "a/b", GzipJsonBlobSerializer())


def test_put_makes_an_item_ready_and_get_locks_it(queue):
    queue.put({"url": "https://example.com"})
    assert len(_names(queue, FSState.READY)) == 1

    item_id, item = queue.get()

    assert item == {"url": "https://example.com"}
    assert _names(queue, FSState.READY) == []
    assert _names(queue, FSState.LOCKED) == [item_id]


def test_get_on_an_empty_queue_returns_nothing(queue):
    assert queue.get() is None


def test_done_and_error_move_a_locked_item(queue):
    queue.put("first")
    queue.put("second")
    first_id, _ = queue.get()
    second_id, _ = queue.get()

    queue.done(first_id)
    queue.error(second_id)

    assert _names(queue, FSState.DONE) == [first_id]
    assert _names(queue, FSState.ERROR) == [second_id]
    assert _names(queue, FSState.LOCKED) == []


def test_unlock_all_returns_locked_items_to_ready(queue):
    queue.put("item")
    item_id, _ = queue.get()

    queue.unlock_all()

    assert _names(queue, FSState.READY) == [item_id]
    assert _names(queue, FSState.LOCKED) == []


def test_corrupt_item_raises_with_its_id(queue):
    queue.put("item")
    (path,) = Path(queue._get_dir(FSState.READY)).iterdir()
    path.write_bytes(b"not gzip")

    with pytest.raises(FSQueueError) as error:
        queue.get()

    assert error.value.item_id == path.name
