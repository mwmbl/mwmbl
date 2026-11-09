"""Fetch each query's top Commons candidates with image search's own client, one cached file per query.

The client sends image search's User-Agent, caps concurrency and records Wikimedia's back-off, which this waits
out before retrying. A query whose file exists is skipped, so a run resumes.
"""

import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Callable, TypeVar

import httpx

from mwmbl.imagesearch.commons import new_client, search_commons
from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage
from mwmbl.rankeval.seed_images import CANDIDATES_DIR, WORKERS
from mwmbl.rankeval.seed_images.queries import load_queries

NUM_CANDIDATES = 20
BACKOFF_SECONDS = 30
MAX_ATTEMPTS = 10
T = TypeVar("T")


def candidates_path(query: str) -> Path:
    return CANDIDATES_DIR / (sha1(query.encode()).hexdigest() + ".json")


def image_to_json(image: SourceImage) -> dict:
    fields = dataclasses.asdict(image)
    return fields | {"categories": list(image.categories), "uploaded_at": image.uploaded_at.isoformat()}


def image_from_json(fields: dict) -> SourceImage:
    parsed = {"categories": tuple(fields["categories"]), "uploaded_at": datetime.fromisoformat(fields["uploaded_at"])}
    return SourceImage(**fields | parsed)


def load_candidates(query: str) -> list[SourceImage]:
    return [image_from_json(fields) for fields in json.loads(candidates_path(query).read_text())["images"]]


def with_backoff(fetch: Callable[[], T]) -> tuple[T, float]:
    """What fetch() returns, and how long the call that returned it took, waiting out each request to back off."""
    for _ in range(MAX_ATTEMPTS):
        start = time.monotonic()
        try:
            result = fetch()
        except ImageSourceUnavailable as e:
            print(f"Commons unavailable ({e}), waiting {BACKOFF_SECONDS}s")
            time.sleep(BACKOFF_SECONDS)
            continue
        return result, time.monotonic() - start
    raise ImageSourceUnavailable(f"Commons still unavailable after {MAX_ATTEMPTS} attempts")


def collect_query(client: httpx.Client, query: str) -> None:
    images, latency = with_backoff(lambda: search_commons(client, query, NUM_CANDIDATES))
    record = {"query": query, "latency_s": latency, "images": [image_to_json(image) for image in images]}
    candidates_path(query).write_text(json.dumps(record))


def main() -> None:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    queries = load_queries()
    remaining = [query for query in queries if not candidates_path(query).exists()]
    print(f"{len(queries) - len(remaining)} queries already collected, {len(remaining)} to go")
    with new_client() as client, ThreadPoolExecutor(WORKERS) as executor:
        list(executor.map(lambda query: collect_query(client, query), remaining))
    print(f"Candidates for {len(queries)} queries -> {CANDIDATES_DIR}")


if __name__ == "__main__":
    main()
