"""Ask Jev the hardened image questions about each query, as Seed Search will once Staan has answered.

    uv run python -m mwmbl.rankeval.seed_images.run_hardened image  # need + relevance in one request, every query
    uv run python -m mwmbl.rankeval.seed_images.run_hardened top1   # relevance given only Staan's top result

A train query's web results are Staan's, in Staan's order (queries.py), which is what production will send; an
en-gb query's are in production's served order, the nearest the eval has.
The candidates are the Jev-safe ones the first runs used (gate_data), so only the questions differ from them.
`top1` asks only about the queries with relevance labels, to choose how many web results to send. Responses
share run_jev's cache, and a run resumes. Score them with `gate_analysis --arm hardened` (or `top1`).
"""

import sys

import django

django.setup()

from mwmbl.imagesearch.judge import image_request, need_score, relevance_request, scores  # noqa: E402
from mwmbl.imagesearch.pipeline import file_id  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.rankeval.seed_images import run_resumably  # noqa: E402
from mwmbl.rankeval.seed_images.gate_analysis import HARDENED_PATH, TOP1_PATH  # noqa: E402
from mwmbl.rankeval.seed_images.gate_data import GATE_DIR, jev_safe_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.queries import load_queries, read_jsonl, web_documents  # noqa: E402
from mwmbl.rankeval.seed_images.run_jev import CACHE_DIR, timed_response  # noqa: E402


def score_image(query: str, entry: dict, images: list[SourceImage]) -> dict:
    response, latency = timed_response(image_request(query, web_documents(entry), images))
    relevance = scores(response, "i", len(images))
    file_ids = [file_id(image) for image in images]
    record = {"query": query, "need": need_score(response), "file_ids": file_ids, "scores": relevance}
    return record | {"latency_s": latency, "usage": response["usage"]}


def score_top1(query: str, entry: dict, images: list[SourceImage]) -> dict:
    top_result = web_documents(entry)[:1]
    response, latency = timed_response(relevance_request(query, top_result, images))
    file_ids = [file_id(image) for image in images]
    return {"query": query, "file_ids": file_ids, "scores": scores(response, "i", len(images)), "latency_s": latency}


def main() -> None:
    queries = load_queries()
    safe = jev_safe_candidates()
    if sys.argv[1] == "image":
        # A query with no Commons candidates is asked its need alone.
        run_resumably(HARDENED_PATH, "query", list(queries), lambda q: score_image(q, queries[q], safe.get(q, [])))
    else:
        labelled = sorted({row["query"] for row in read_jsonl(GATE_DIR / "labels_relevance.jsonl")})
        run_resumably(TOP1_PATH, "query", labelled, lambda q: score_top1(q, queries[q], safe[q]))


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    main()
