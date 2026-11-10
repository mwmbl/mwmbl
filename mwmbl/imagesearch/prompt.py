"""The questions a judge model is asked about Commons images, and the text it is shown of each.

Each question names its inputs in backticks; judge.py supplies them alongside the question. The wording is the
Seed Search images evaluation's (mwmbl/rankeval/seed-search-images-eval.md on the wikicommons_image_search
evaluation branch), word for word, since its thresholds were measured with exactly these questions:

- NSFW: the safety benchmark's text-only question on mwmbl/mwmbl#610.
- Image relevance: how good an image is as a result for the query.
- Image need: would images improve this query's results page at all?
"""

from mwmbl.imagesearch.types import SourceImage

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


def image_text(image: SourceImage) -> str:
    description = image.description[:DESCRIPTION_CHARS]
    categories = " | ".join(image.categories)
    return f"File name: {image.title}\nDescription: {description}\nCategories: {categories}"


def numbered_titles(web_titles: list[str]) -> str:
    return "\n".join(f"{position}. {title}" for position, title in enumerate(web_titles, start=1))
