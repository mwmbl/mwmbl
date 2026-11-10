"""Ask Jev, in separate mode, how good each Jev-safe candidate is as an image result for its query.

One request per query covers all its Jev-safe candidates. Responses share run_jev's cache, and each request's
latency is recorded with its scores. A run resumes.
"""

import django

django.setup()

from mwmbl.imagesearch.judge import relevance_request, scores  # noqa: E402
from mwmbl.imagesearch.pipeline import file_id  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.rankeval.seed_images import run_resumably  # noqa: E402
from mwmbl.rankeval.seed_images.gate_data import GATE_DIR, RELEVANCE_PATH, jev_safe_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.run_jev import CACHE_DIR, timed_response  # noqa: E402


def score_relevance(query: str, images: list[SourceImage]) -> dict:
    response, latency = timed_response(relevance_request(query, images))
    file_ids = [file_id(image) for image in images]
    return {"query": query, "file_ids": file_ids, "scores": scores(response, "i", len(images)), "latency_s": latency}


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = {query: images for query, images in jev_safe_candidates().items() if images}
    run_resumably(RELEVANCE_PATH, "query", list(safe), lambda query: score_relevance(query, safe[query]))


if __name__ == "__main__":
    main()
