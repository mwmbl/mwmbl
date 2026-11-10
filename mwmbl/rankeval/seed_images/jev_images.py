"""The combined request: production's web questions and every image question, in the one request Seed Search
would send. The evaluation compared it with sending the image questions separately, as mwmbl.imagesearch.judge
does; the questions themselves are in mwmbl.imagesearch.prompt.
"""

from mwmbl.imagesearch.judge import nsfw_questions, query_state, relevance_questions, staan_need_question
from mwmbl.imagesearch.prompt import NSFW_POLICY
from mwmbl.imagesearch.types import SourceImage
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import jev_request


def combined_request(query: str, documents: list[Document], staan: list[Document], images: list[SourceImage]) -> dict:
    web = jev_request(query, documents)
    image_questions = relevance_questions(images) | nsfw_questions(images) | staan_need_question(staan)
    state = query_state(query, staan) | {"policy": NSFW_POLICY}
    return web | {"state": state, "questions": web["questions"] | image_questions}
