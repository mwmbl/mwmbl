"""The combined request: production's web questions and every image question, in the one request Seed Search
would send. The evaluation compared it with sending the image questions separately, as mwmbl.imagesearch.judge
does; the questions themselves are in mwmbl.imagesearch.prompt.
"""

from mwmbl.imagesearch.judge import need_question, nsfw_questions, relevance_questions
from mwmbl.imagesearch.prompt import NSFW_POLICY
from mwmbl.imagesearch.types import SourceImage
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.jev_rank import jev_request


def combined_request(query: str, documents: list[Document], web_titles: list[str], images: list[SourceImage]) -> dict:
    # The need question sees the served top ten's titles, which the pool's order is not yet.
    web = jev_request(query, documents)
    questions = web["questions"] | relevance_questions(images) | nsfw_questions(images) | need_question(web_titles)
    return web | {"state": web["state"] | {"policy": NSFW_POLICY}, "questions": questions}
