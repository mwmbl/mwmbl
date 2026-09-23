"""Train the Combined Search model: the combined config, plus Staan's own ranking as features.

Staan's ordering used to reach the combined model only through `item_score`
(``STAAN_TOP_SCORE - rank``), a column it shares with the index's own scores. This trains the
same config with the Rust pipeline's provider features switched on, so the model can learn
how far to trust Staan's order:

- ``in_staan``: 1 when Staan returned this URL for the query, else 0.
- ``staan_rank``: Staan's 0-based rank for the URL, missing when Staan did not return it.

Both are keyed on the URL, not on a document's source, so a page the index and Staan both
return carries them whichever copy is scored. The feature definitions live in
``mwmbl_rank`` (``StaanRank``); this module only supplies each row's Staan rank.

Staan ranks come from the Pass-2 checkpoint, which covers the 849 LLM-labelled queries. Rows
for any other query (most extension and curation rows) never had Staan asked, so both
features are missing for them rather than "not in Staan" - XGBoost learns a default
direction for them.

Usage::

    DATABASE_URL="postgres://daoud@" uv run --no-sync python -m mwmbl.rankeval.ltr.provider_features \\
        --save-model devdata/rankeval-2026-04/model-combined-provider.xgb
    cp devdata/rankeval-2026-04/model-combined-provider.xgb mwmbl/resources/model-combined.xgb
"""

import json
from argparse import ArgumentParser
from pathlib import Path

import pandas as pd

from mwmbl.rankeval.ltr.llm_experiment import (
    FEATURE_COLUMNS,
    build_training_frame,
    load_curation_frame,
    load_datasets,
)
from mwmbl.rankeval.paths import ROOT_DIR
from mwmbl.tinysearchengine.ltr import RustXGBPipeline

STAAN_CHECKPOINT = ROOT_DIR / "devdata" / "llm_relabel" / "pass2_staan.jsonl"

# The config model-combined.xgb was first trained with (see mwmbl/rankeval/README.md), so
# the provider model differs from it only by the features.
OVERALL_THRESHOLD = 4.0
EXT_WEIGHT = 0.25
CURATION_WEIGHT = 0.5
WEAK_NEG_WEIGHT = 0.25
SCALE_POS_WEIGHT = 1.0
REG_LAMBDA = 2.0
NUM_ROUNDS = 100


def load_staan_ranks() -> dict[str, dict[str, int]]:
    """qnorm -> {url: Staan's 0-based rank}, for the 849 LLM-labelled queries."""
    ranks = {}
    with open(STAAN_CHECKPOINT) as f:
        for line in f:
            record = json.loads(line)
            ranks[record["query"].lower().strip()] = {r["url"]: i for i, r in enumerate(record["results"])}
    return ranks


def training_frame() -> pd.DataFrame:
    """The combined model's training rows: LLM + extension + curation, no held-out split,
    with each row's Staan rank."""
    ext, llm = load_datasets()
    curation = load_curation_frame(WEAK_NEG_WEIGHT)
    frame = build_training_frame(
        "mixed",
        ext,
        llm,
        train_queries=set(llm["qnorm"]),
        test_queries=set(),
        overall_threshold=OVERALL_THRESHOLD,
        ext_weight=EXT_WEIGHT,
        ext_downsample=None,
        seed=0,
        curation=curation,
        curation_weight=CURATION_WEIGHT,
    )

    by_query = load_staan_ranks()
    query_ranks = [by_query.get(query.lower().strip()) for query in frame["query"]]
    frame["staan_asked"] = [ranks is not None for ranks in query_ranks]
    frame["staan_rank"] = [None if ranks is None else ranks.get(url) for url, ranks in zip(frame["url"], query_ranks)]
    in_staan = frame["staan_rank"].notna()
    print(f"Rows with Staan asked: {frame['staan_asked'].sum()} / {len(frame)}; in Staan's results: {in_staan.sum()}")
    return frame


def train() -> RustXGBPipeline:
    frame = training_frame()
    # Labels are already binary; threshold=0.5 makes the Rust-side binarisation a no-op.
    model = RustXGBPipeline(
        threshold=0.5,
        scale_pos_weight=SCALE_POS_WEIGHT,
        reg_lambda=REG_LAMBDA,
        num_rounds=NUM_ROUNDS,
        provider_features=True,
    )
    model.fit(frame[FEATURE_COLUMNS + ["staan_asked", "staan_rank"]], frame["label"], sample_weight=frame["weight"])
    return model


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--save-model", required=True, help="where to write the model (.xgb)")
    args = parser.parse_args()

    model = train()
    Path(args.save_model).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(args.save_model)
    print(f"Saved to {args.save_model}")


if __name__ == "__main__":
    run()
