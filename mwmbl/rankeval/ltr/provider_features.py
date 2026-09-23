"""The Combined Search model with Staan's own ranking as a feature.

The deployed combined model has no source feature: Staan's ordering reaches it only through
`item_score` (``STAAN_TOP_SCORE - rank``), a column it shares with the index's own scores and
Wikipedia's. This trains the same model with two features added, so it can learn how far to
trust Staan's order:

- ``in_staan``: 1 when Staan returned this URL for the query, else 0.
- ``staan_rank``: Staan's 0-based rank for the URL, missing when Staan did not return it.

Both are keyed on the URL, not on a document's source, so a page the index and Staan both
return carries them whichever copy is scored - at training time the pool has one row per
URL, and at serving time both copies are candidates.

The extra features have no home in the Rust pipeline yet, so this is a Python XGBoost model
over the Rust feature matrix (``RustXGBPipeline.extract_features``) plus the extra columns,
trained with the Rust pipeline's exact parameters. ``--no-provider-features`` trains the
control on the same rows and code path, so the two models differ by the features alone.

Staan ranks come from the Pass-2 checkpoint, which covers the 849 LLM-labelled queries. Rows
for any other query (most extension and curation rows) never had Staan asked, so their
provider features are missing rather than "not in Staan" - XGBoost learns a default
direction for them.

Usage::

    DATABASE_URL="postgres://daoud@" uv run --no-sync python -m mwmbl.rankeval.ltr.provider_features \\
        --save-model devdata/rankeval-2026-04/model-combined-provider.json
    DATABASE_URL="postgres://daoud@" uv run --no-sync python -m mwmbl.rankeval.ltr.provider_features \\
        --no-provider-features --save-model devdata/rankeval-2026-04/model-combined-control.json
"""

import json
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from mwmbl.rankeval.ltr.llm_experiment import (
    FEATURE_COLUMNS,
    build_training_frame,
    load_curation_frame,
    load_datasets,
)
from mwmbl.rankeval.paths import ROOT_DIR
from mwmbl_rank import RustXGBPipeline as RustPipeline

STAAN_CHECKPOINT = ROOT_DIR / "devdata" / "llm_relabel" / "pass2_staan.jsonl"
PROVIDER_FEATURE_NAMES = ["in_staan", "staan_rank"]

# The config model-combined.xgb was trained with (see mwmbl/rankeval/README.md), so the
# control reproduces it and the provider model differs from it only by the features.
OVERALL_THRESHOLD = 4.0
EXT_WEIGHT = 0.25
CURATION_WEIGHT = 0.5
WEAK_NEG_WEIGHT = 0.25
XGB_PARAMS = {
    "objective": "binary:logistic",
    "tree_method": "exact",
    "reg_lambda": 2.0,
    "scale_pos_weight": 1.0,
}
NUM_ROUNDS = 100

# Indices into the Rust feature row, for the majority-terms filter RustXGBPipeline.predict
# applies. Named by the Rust side's FEATURE_NAMES; checked on import below.
NUM_TERMS_INDEX = 42
MATCH_TERMS_INDEX = 49
assert RustPipeline.feature_names()[NUM_TERMS_INDEX] == "num_terms"
assert RustPipeline.feature_names()[MATCH_TERMS_INDEX] == "match_terms"


def provider_features(urls: list[str], staan_ranks: dict[str, int] | None) -> np.ndarray:
    """The ``in_staan`` and ``staan_rank`` columns. ``staan_ranks`` None means Staan was never asked."""
    if staan_ranks is None:
        return np.full((len(urls), len(PROVIDER_FEATURE_NAMES)), np.nan, dtype=np.float32)
    in_staan = [1.0 if url in staan_ranks else 0.0 for url in urls]
    rank = [staan_ranks.get(url, np.nan) for url in urls]
    return np.column_stack([in_staan, rank]).astype(np.float32)


def feature_matrix(records: list[dict], staan_ranks: list[dict[str, int] | None] | None) -> np.ndarray:
    """The Rust features, plus the provider features when ``staan_ranks`` (one per record) is given."""
    base = np.array(RustPipeline.extract_features(records), dtype=np.float32)
    if staan_ranks is None:
        return base
    extra = np.vstack([provider_features([r["url"]], ranks) for r, ranks in zip(records, staan_ranks)])
    return np.hstack([base, extra])


def fails_term_filter(features: np.ndarray) -> np.ndarray:
    """Rows RustXGBPipeline.predict zeroes: matching no more than half the query terms."""
    return features[:, MATCH_TERMS_INDEX] <= features[:, NUM_TERMS_INDEX] / 2.0


def load_staan_ranks() -> dict[str, dict[str, int]]:
    """qnorm -> {url: Staan's 0-based rank}, for the 849 LLM-labelled queries."""
    ranks = {}
    with open(STAAN_CHECKPOINT) as f:
        for line in f:
            record = json.loads(line)
            ranks[record["query"].lower().strip()] = {r["url"]: i for i, r in enumerate(record["results"])}
    return ranks


def training_frame() -> pd.DataFrame:
    """The combined model's training rows: LLM + extension + curation, no held-out split."""
    ext, llm = load_datasets()
    curation = load_curation_frame(WEAK_NEG_WEIGHT)
    return build_training_frame(
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


def train(with_provider_features: bool) -> xgb.Booster:
    frame = training_frame()
    records = frame[FEATURE_COLUMNS].to_dict("records")
    staan_ranks = None
    if with_provider_features:
        by_query = load_staan_ranks()
        staan_ranks = [by_query.get(r["query"].lower().strip()) for r in records]
        covered = [ranks is not None for ranks in staan_ranks]
        in_staan = [ranks is not None and r["url"] in ranks for r, ranks in zip(records, staan_ranks)]
        print(f"Rows with Staan asked: {sum(covered)} / {len(records)}; in Staan's results: {sum(in_staan)}")

    features = feature_matrix(records, staan_ranks)
    matrix = xgb.DMatrix(features, label=frame["label"].to_numpy(), weight=frame["weight"].to_numpy())
    print(f"Training on {features.shape[0]} rows x {features.shape[1]} features")
    return xgb.train(XGB_PARAMS, matrix, num_boost_round=NUM_ROUNDS)


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--save-model", required=True, help="where to write the booster (.json)")
    parser.add_argument("--no-provider-features", action="store_true", help="train the control: the Rust features only")
    args = parser.parse_args()

    booster = train(with_provider_features=not args.no_provider_features)
    Path(args.save_model).parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(args.save_model)
    print(f"Saved to {args.save_model}")


if __name__ == "__main__":
    run()
