from datetime import datetime
from queue import Queue

from mwmbl.crawler.urls import FoundURL, URLStatus
from mwmbl.url_queue import URLQueue


def test_url_queue_empties():
    new_item_queue = Queue()
    queued_batches = Queue()

    url_queue = URLQueue(new_item_queue, queued_batches, min_top_domains=1)
    new_item_queue.put([FoundURL("https://google.com", "123", 10.0, URLStatus.NEW.value, datetime(2023, 1, 19))])

    url_queue.update()

    items = queued_batches.get(block=False)

    assert items == ["https://google.com"]


def test_url_queue_multiple_puts():
    new_item_queue = Queue()
    queued_batches = Queue()

    url_queue = URLQueue(new_item_queue, queued_batches, min_top_domains=1)
    new_item_queue.put([FoundURL("https://google.com", "123", 10.0, URLStatus.NEW.value, datetime(2023, 1, 19))])
    url_queue.update()

    new_item_queue.put([FoundURL("https://www.supermemo.com", "124", 10.0, URLStatus.NEW.value, datetime(2023, 1, 20))])
    url_queue.update()

    items = queued_batches.get(block=False)
    assert items == ["https://google.com"]

    items_2 = queued_batches.get(block=False)
    assert items_2 == ["https://www.supermemo.com"]
