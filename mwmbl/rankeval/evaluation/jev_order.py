"""Order candidate pools offline with Jev composite + Staan rank, as Combined Search serves.

Asks Jev the production questions (`mwmbl.tinysearchengine.jev_rank`), one request per
query, and sorts each pool with the production weights, so an arm built here is the ordering
users get. Pair its output with `holistic_judge` to compare it with another arm.

The candidates file is JSON mapping each query to its pool, each candidate a {"url",
"title", "extract", "staan_position"} object, where `staan_position` is the 0-based rank
Staan gave it, or null for an index result. The output is a lists file for
`holistic_judge`: each query's pool in composite order, each result with Jev's `relevance`
and `quality` added.

Unlike serving, a request here waits for Jev and retries when it is rate-limited, and each
response is cached in WORK by its request body, so an interrupted run resumes and a rerun
costs nothing.

    JEV_API_KEY=... DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \\
        uv run python -m mwmbl.rankeval.evaluation.jev_order candidates.json out.json work/
"""

import json
import time
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from pathlib import Path

import django
import requests
from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.jev_rank import composite_order, jev_request, scores_from
from mwmbl.tinysearchengine.staan import staan_score

TIMEOUT_SECONDS = 120
RETRY_STATUSES = {429, 529}
MAX_ATTEMPTS = 6


def document_for(candidate: dict) -> Document:
    if candidate["staan_position"] is None:
        return Document(candidate["title"], candidate["url"], candidate["extract"])
    score = staan_score(candidate["staan_position"])
    return Document(candidate["title"], candidate["url"], candidate["extract"], score, source=DocumentSource.STAAN)


def call_jev(body: dict) -> dict:
    headers = {"Authorization": f"Bearer {settings.JEV_API_KEY}"}
    for attempt in range(MAX_ATTEMPTS):
        response = requests.post(settings.JEV_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
        if response.status_code not in RETRY_STATUSES:
            break
        time.sleep(2**attempt)
    response.raise_for_status()
    return response.json()


def cached_response(body: dict, work: Path) -> dict:
    cache_path = work / (sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    response = call_jev(body)
    cache_path.write_text(json.dumps(response))
    return response


def order_query(query: str, candidates: list[dict], work: Path) -> list[dict]:
    documents = [document_for(candidate) for candidate in candidates]
    jev_scores = scores_from(cached_response(jev_request(query, documents), work), len(documents))
    by_url = {
        candidate["url"]: dict(candidate, relevance=float(relevance), quality=float(quality))
        for candidate, (relevance, quality) in zip(candidates, jev_scores)
    }
    return [by_url[document.url] for document in composite_order(documents, jev_scores)]


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    django.setup()
    assert settings.JEV_API_KEY, "JEV_API_KEY is not set"
    pools = json.loads(args.candidates.read_text())
    args.work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.workers) as executor:
        ordered = list(executor.map(lambda query: order_query(query, pools[query], args.work), pools))
    args.out.write_text(json.dumps(dict(zip(pools, ordered))))
    print(f"{len(pools)} queries ordered -> {args.out}")


if __name__ == "__main__":
    main()
