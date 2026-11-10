"""Jev questions about Commons images, and requests in the format `jev_rank.jev_request` sends.

- NSFW: the safety benchmark's text-only question on mwmbl/mwmbl#610, word for word, as its thresholds were
  measured with it. Sent alone, per query's candidates, it is the "separate" mode.
- Image relevance, and image need: would images improve this query's results page at all?
- Combined: production's web questions plus all of the above, in the one request Seed Search would send.
"""

from django.conf import settings

from mwmbl.imagesearch.types import SourceImage
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import SEARCHER, jev_request

DESCRIPTION_CHARS = 1500

NSFW_POLICY = (
    "An image search that must never show nudity of any kind. Nude or topless figures in paintings, sculpture, "
    "drawings, illustration, anime and medical images count, as well as photographs."
)
NSFW_QUESTION = (
    "Judging only by its Wikimedia Commons file name, description and categories, how likely is it that the "
    "image `image` shows nudity under `policy`?"
)
NSFW_CRITERIA = [
    "Almost certainly not: the text describes a subject that would not show a bare body.",
    "Unlikely: a person or body may appear, but nothing suggests they are unclothed.",
    "Possible: swimwear, underwear, a classical or mythological figure, a bather, a medical subject, or other "
    "text that leaves nudity open.",
    "Likely or certain: the text says or strongly implies bare breasts, buttocks or genitals, in any medium.",
]

IMAGE_RELEVANCE_QUESTION = (
    "Judging by its Wikimedia Commons file name, description and categories, how well would the image `image`, "
    "shown on the search results page for `query`, serve `searcher`?"
)
IMAGE_RELEVANCE_CRITERIA = [
    "Irrelevant: a different subject, the wrong sense of an ambiguous query, or a picture that adds nothing.",
    "Marginal: loosely related - a tangential subject, a generic or low-information picture, a map or diagram "
    "of something else, or a detail that would puzzle the searcher.",
    "Good: shows the query's subject, but is not the picture the searcher would most want to see.",
    "Excellent: exactly the picture the searcher would want - the person, place, thing or species the query "
    "names, clearly shown.",
]

IMAGE_NEED_QUESTION = (
    "Would a row of pictures from Wikimedia Commons improve the search results page for `query`, for "
    "`searcher`, given the web results `results` the page already shows?"
)
IMAGE_NEED_CRITERIA = [
    "No: pictures would not help - a navigational, transactional, local, how-to or abstract query, or one "
    "where Commons would have nothing apt.",
    "Slightly: a picture might be a small extra, but the searcher came for something else.",
    "Yes: pictures would be a useful part of the page - a person, place, building, artwork, species or object "
    "the searcher may want to see.",
    "Strongly: seeing it is the main point - the query is mostly a request to see what something looks like.",
]
NEED_KEY = "need"


def image_text(image: SourceImage) -> str:
    description = image.description[:DESCRIPTION_CHARS]
    categories = " | ".join(image.categories)
    return f"File name: {image.title}\nDescription: {description}\nCategories: {categories}"


def score_question(name: str, value: str, question: str, criteria: list[str]) -> dict:
    return {"type": "score", "instructions": {name: value, "question": question}, "criteria": criteria}


def image_questions(prefix: str, images: list[SourceImage], question: str, criteria: list[str]) -> dict:
    return {
        f"{prefix}{i}": score_question("image", image_text(image), question, criteria) for i, image in enumerate(images)
    }


def need_question(web_titles: list[str]) -> dict:
    results = "\n".join(f"{position}. {title}" for position, title in enumerate(web_titles, start=1))
    return {NEED_KEY: score_question("results", results, IMAGE_NEED_QUESTION, IMAGE_NEED_CRITERIA)}


def nsfw_request(images: list[SourceImage]) -> dict:
    return {
        "model": settings.JEV_MODEL,
        "state": {"policy": NSFW_POLICY},
        "questions": image_questions("n", images, NSFW_QUESTION, NSFW_CRITERIA),
    }


def need_request(query: str, web_titles: list[str]) -> dict:
    state = {"query": query, "searcher": SEARCHER}
    return {"model": settings.JEV_MODEL, "state": state, "questions": need_question(web_titles)}


def relevance_request(query: str, images: list[SourceImage]) -> dict:
    state = {"query": query, "searcher": SEARCHER}
    questions = image_questions("i", images, IMAGE_RELEVANCE_QUESTION, IMAGE_RELEVANCE_CRITERIA)
    return {"model": settings.JEV_MODEL, "state": state, "questions": questions}


def combined_request(query: str, documents: list[Document], web_titles: list[str], images: list[SourceImage]) -> dict:
    # The need question sees the served top ten's titles, which the pool's order is not yet.
    web = jev_request(query, documents)
    relevance = image_questions("i", images, IMAGE_RELEVANCE_QUESTION, IMAGE_RELEVANCE_CRITERIA)
    nsfw = image_questions("n", images, NSFW_QUESTION, NSFW_CRITERIA)
    questions = web["questions"] | relevance | nsfw | need_question(web_titles)
    return web | {"state": web["state"] | {"policy": NSFW_POLICY}, "questions": questions}


def scores(response: dict, prefix: str, count: int) -> list[float]:
    answers = response["answers"]
    return [answers[f"{prefix}{i}"]["score"] for i in range(count)]


def need_score(response: dict) -> float:
    return response["answers"][NEED_KEY]["score"]
