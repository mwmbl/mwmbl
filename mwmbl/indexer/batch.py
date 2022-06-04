from itertools import islice
from typing import Iterator


def grouper(n: int, iterator: Iterator):
    while True:
        chunk = tuple(islice(iterator, n))
        if not chunk:
            return
        yield chunk