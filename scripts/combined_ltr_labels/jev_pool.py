"""The ndcg+new ranking to depth 30 on the en-gb queries, for the Jev arms to fill from.

`engb_staan_experiment.py rank` keeps only each arm's top ten, but a Jev fill or re-rank
needs the candidates below it. This trains ndcg+new exactly as that command does and ranks
a fresh retrieval with and without MMR, to depth 30, into engb_jev_pool.json.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import pandas as pd
from django.conf import settings

from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from scripts.combined_ltr_labels.engb_eval import SHIPPED, BoosterModel, rank_all
from scripts.combined_ltr_labels.engb_staan_experiment import LABELS, load_engb
from scripts.combined_ltr_labels.objective_experiment import features, train

POOL = LABELS / "engb_jev_pool.json"
DEPTH = 30

if __name__ == "__main__":
    llm, ext, new, *_ = load_engb()
    frames = [llm, ext, new]
    booster = train("ndcg", pd.concat(frames, ignore_index=True), np.concatenate([features(f) for f in frames]))
    models = {
        SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH)),
        "ndcg+new": BoosterModel(booster),
    }
    rank_all(models, POOL, keep=DEPTH, no_mmr=("ndcg+new",))
