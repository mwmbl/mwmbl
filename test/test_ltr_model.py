import math

from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import score_documents


def test_the_configured_model_loads_and_scores():
    model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
    documents = [
        Document("python", "https://python.example.com/", "the python programming language"),
        Document("Python_logo", "https://commons.wikimedia.org/wiki/File:Python_logo.jpg", "the python logo"),
    ]

    scores = score_documents(model, "python", documents)

    assert len(scores) == 2
    assert all(math.isfinite(score) for score in scores)
