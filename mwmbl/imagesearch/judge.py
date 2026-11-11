"""Asks a judge model the questions in prompt.py about Commons images.

The judge is Jev (TypeSafe's structured-judgment model), sent requests in the format and over the
connection Combined Search's ranking uses (see tinysearchengine.jev_rank). Only this module knows that, so
moving to another judge changes nothing outside it.

Each question is a Score question: the answer is a number from 0 to one less than the number of criteria.

Only the NSFW scores are cached, since that request carries no query. Relevance and need answers depend on the
user's query, and nothing that does is ever stored.
"""

import hashlib
import json
import math

from django.conf import settings

from mwmbl.imagesearch import verdict_cache
from mwmbl.imagesearch.prompt import (
    IMAGE_NEED_CRITERIA,
    IMAGE_NEED_QUESTION,
    IMAGE_RELEVANCE_CRITERIA,
    IMAGE_RELEVANCE_QUESTION,
    NEED_WEB_RESULTS,
    NSFW_CRITERIA,
    NSFW_POLICY,
    NSFW_QUESTION,
    RELEVANCE_WEB_RESULTS,
    WEB_SNIPPET_CHARS,
    WEB_TITLE_CHARS,
    WEB_URL_CHARS,
    image_text,
    numbered_titles,
    web_text,
)
from mwmbl.imagesearch.types import SourceImage
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import SEARCHER, post

NSFW_PREFIX = "n"
RELEVANCE_PREFIX = "i"
NEED_KEY = "need"
NSFW_CACHE_KIND = "judge-nsfw"


def score_question(name: str, value: str, question: str, criteria: list[str]) -> dict:
    return {"type": "score", "instructions": {name: value, "question": question}, "criteria": criteria}


def image_questions(prefix: str, images: list[SourceImage], question: str, criteria: list[str]) -> dict:
    return {
        f"{prefix}{i}": score_question("image", image_text(image), question, criteria) for i, image in enumerate(images)
    }


def nsfw_questions(images: list[SourceImage]) -> dict:
    return image_questions(NSFW_PREFIX, images, NSFW_QUESTION, NSFW_CRITERIA)


def relevance_questions(images: list[SourceImage]) -> dict:
    return image_questions(RELEVANCE_PREFIX, images, IMAGE_RELEVANCE_QUESTION, IMAGE_RELEVANCE_CRITERIA)


def need_question(web_titles: list[str]) -> dict:
    results = numbered_titles(web_titles)
    return {NEED_KEY: score_question("results", results, IMAGE_NEED_QUESTION, IMAGE_NEED_CRITERIA)}


def staan_need_question(staan: list[Document]) -> dict:
    return need_question([document.title for document in staan[:NEED_WEB_RESULTS]])


def query_state(query: str, staan: list[Document]) -> dict:
    """What the query-dependent questions share: the query, and Staan's top results, to say which sense is meant."""
    top_results = [
        {
            "title": web_text(document.title, WEB_TITLE_CHARS),
            "url": web_text(document.url, WEB_URL_CHARS),
            "snippet": web_text(document.extract or "", WEB_SNIPPET_CHARS),
        }
        for document in staan[:RELEVANCE_WEB_RESULTS]
    ]
    return {"query": query, "searcher": SEARCHER, "top_results": top_results}


def nsfw_request(images: list[SourceImage]) -> dict:
    return {"model": settings.JEV_MODEL, "state": {"policy": NSFW_POLICY}, "questions": nsfw_questions(images)}


def need_request(query: str, staan: list[Document]) -> dict:
    return {"model": settings.JEV_MODEL, "state": query_state(query, staan), "questions": staan_need_question(staan)}


def relevance_request(query: str, staan: list[Document], images: list[SourceImage]) -> dict:
    return {"model": settings.JEV_MODEL, "state": query_state(query, staan), "questions": relevance_questions(images)}


def image_request(query: str, staan: list[Document], images: list[SourceImage]) -> dict:
    """The one request Seed Search sends once Staan has answered: the query's image need and every candidate's
    relevance. It doesn't wait for the NSFW scores, so it asks about every candidate."""
    questions = staan_need_question(staan) | relevance_questions(images)
    return {"model": settings.JEV_MODEL, "state": query_state(query, staan), "questions": questions}


def scores(response: dict, prefix: str, count: int) -> list[float]:
    answers = response["answers"]
    return [answers[f"{prefix}{i}"]["score"] for i in range(count)]


def need_score(response: dict) -> float:
    return response["answers"][NEED_KEY]["score"]


def image_scores(query: str, staan: list[Document], images: list[SourceImage]) -> tuple[float, list[float]]:
    """The query's image need and each image's relevance, in order, from one request. Raises on any failure,
    including an answer off its question's scale."""
    response = post(image_request(query, staan, images), settings.IMAGE_JUDGE_TIMEOUT_SECONDS)
    need = checked_score(need_score(response), IMAGE_NEED_CRITERIA)
    relevance = [
        checked_score(score, IMAGE_RELEVANCE_CRITERIA) for score in scores(response, RELEVANCE_PREFIX, len(images))
    ]
    return need, relevance


def nsfw_fingerprint() -> str:
    """Changes whenever the model, the NSFW question or the request's shape does, so a cached score is only
    reused for exactly the question that gave it. The images' text is in each key, not here."""
    shape = {"request": nsfw_request([]), "question": score_question("image", "", NSFW_QUESTION, NSFW_CRITERIA)}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def nsfw_cache_key(fingerprint: str, image: SourceImage) -> str:
    # The text is what the judge read; the SHA-1 means a replaced file is asked about afresh even if its
    # text is unchanged, so a score is only ever filed under the revision it was given for.
    content = f"{image.sha1}\n{image_text(image)}"
    return verdict_cache.cache_key(NSFW_CACHE_KIND, fingerprint, content)


def checked_score(score: float, criteria: list[str]) -> float:
    # A malformed answer must neither pass an image nor be cached, so anything off the question's scale raises.
    max_score = len(criteria) - 1
    if not (type(score) in (int, float) and math.isfinite(score) and 0 <= score <= max_score):
        raise ValueError(f"Score off the 0-{max_score} scale: {score!r}")
    return float(score)


def nsfw_scores(images: list[SourceImage]) -> list[float]:
    """Each image's NSFW score, in order: from 0, the text almost certainly shows no nudity,
    to the top of its scale (3), it does.

    Fails closed: either every image gets a score the judge gave, or this raises, so a caller can never
    mistake a failure for a low score. Images already scored are read from the verdict cache; the rest
    are asked about in one request, and their scores are cached only once all of them are valid.
    """
    fingerprint = nsfw_fingerprint()
    keys = [nsfw_cache_key(fingerprint, image) for image in images]
    cached = verdict_cache.get_scores(keys)
    unscored = {key: image for key, image in zip(keys, images) if key not in cached}
    if unscored:
        unscored_images = list(unscored.values())
        response = post(nsfw_request(unscored_images), settings.IMAGE_JUDGE_TIMEOUT_SECONDS)
        answered = scores(response, NSFW_PREFIX, len(unscored_images))
        asked = {key: checked_score(score, NSFW_CRITERIA) for key, score in zip(unscored, answered)}
        verdict_cache.put_scores(asked)
        cached |= asked
    return [cached[key] for key in keys]
