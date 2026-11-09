"""Ask Jev, in separate mode, the NSFW question about every candidate and the image-need question about every query.

One NSFW request covers a query's candidates (up to 20); one image-need request sees its web top ten. Responses
are cached by body, as `jev_order` caches them, and each request's latency is recorded with its scores. Queries
already in an output file are skipped, so a run resumes and latencies are those of real calls.
"""

import time

import django
from django.conf import settings

# jev_rank reaches Django models through rank -> blacklist_snapshot, so the registry must be up first.
django.setup()

from mwmbl.imagesearch.pipeline import file_id  # noqa: E402
from mwmbl.rankeval.evaluation.jev_order import cached_response  # noqa: E402
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR, run_resumably  # noqa: E402
from mwmbl.rankeval.seed_images.collect import load_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.jev_images import need_request, need_score, nsfw_request, scores  # noqa: E402
from mwmbl.rankeval.seed_images.queries import load_queries  # noqa: E402

CACHE_DIR = SEED_IMAGES_DIR / "jev_cache"
NSFW_PATH = SEED_IMAGES_DIR / "jev_nsfw.jsonl"
NEED_PATH = SEED_IMAGES_DIR / "jev_need.jsonl"


def timed_response(body: dict) -> tuple[dict, float]:
    start = time.monotonic()
    response = cached_response(body, CACHE_DIR)
    return response, time.monotonic() - start


def score_nsfw(query: str) -> dict:
    images = load_candidates(query)
    response, latency = timed_response(nsfw_request(images))
    nsfw_scores = scores(response, "n", len(images))
    file_ids = [file_id(image) for image in images]
    return {"query": query, "size": len(images), "file_ids": file_ids, "scores": nsfw_scores, "latency_s": latency}


def score_need(query: str, web: list[dict]) -> dict:
    web_titles = [result["title"] for result in web]
    response, latency = timed_response(need_request(query, web_titles))
    return {"query": query, "score": need_score(response), "latency_s": latency}


def main() -> None:
    assert settings.JEV_API_KEY, "JEV_API_KEY is not set: source .env"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    queries = load_queries()
    with_candidates = [query for query in queries if load_candidates(query)]
    run_resumably(NSFW_PATH, "query", with_candidates, score_nsfw)
    run_resumably(NEED_PATH, "query", list(queries), lambda query: score_need(query, queries[query]["web"]))


if __name__ == "__main__":
    main()
