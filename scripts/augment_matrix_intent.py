"""Offline shortcut: append the query-only intent one-hot columns to an existing
Super Search reward matrix, with NO network rebuild.

The intent features (``features.classify_intent``) depend only on the query text,
which is already stored in the matrix's ``.json`` sidecar, so we recompute them for
every stored query and broadcast across all sources. ``R``/``mask`` are untouched.
This avoids re-running ``super_search_eval.py build-matrix`` (~20 min, networked)
just to test a query-only feature.

Usage:
  DATABASE_URL=... DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python \
      scripts/augment_matrix_intent.py --in devdata/ss_eval_matrix \
                                        --out devdata/ss_eval_matrix_intent
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
import django  # noqa: E402
django.setup()

import numpy as np  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix  # noqa: E402
from mwmbl.tinysearchengine.super_search_select.features import (  # noqa: E402
    FEATURE_NAMES, INTENT_NAMES, classify_intent,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="devdata/ss_eval_matrix")
    ap.add_argument("--out", dest="out", default="devdata/ss_eval_matrix_intent")
    args = ap.parse_args()

    m = RewardMatrix.load(args.inp)
    Q, S, F = m.X.shape
    intent_names = [f"intent_{n}" for n in INTENT_NAMES]
    assert m.feature_names == FEATURE_NAMES[:F], (
        f"matrix feature_names {m.feature_names} are not the prefix of the current "
        f"FEATURE_NAMES; refusing to append (rebuild via build-matrix instead).")
    assert FEATURE_NAMES[F:] == intent_names, "intent block must be the appended tail"

    # (Q, n_intents) one-hot, then broadcast across the S source axis.
    per_query = np.array([classify_intent(q) for q in m.queries], dtype=m.X.dtype)
    intent_block = np.broadcast_to(per_query[:, None, :], (Q, S, len(INTENT_NAMES)))
    X_aug = np.concatenate([m.X, intent_block], axis=2)

    aug = RewardMatrix(queries=m.queries, sources=m.sources,
                       feature_names=list(FEATURE_NAMES),
                       X=X_aug, R=m.R, mask=m.mask)
    aug.save(args.out)

    fired = per_query.sum(axis=0)
    print(f"in : {args.inp}  ({Q}x{S}x{F})")
    print(f"out: {args.out}  ({Q}x{S}x{X_aug.shape[2]})")
    print(f"queries with >=1 intent tag: {int((per_query.sum(axis=1) > 0).sum())}/{Q}")
    print(f"per-intent fire count (out of {Q} queries):")
    for n, c in zip(INTENT_NAMES, fired):
        print(f"  intent_{n:10s} {int(c):4d}  ({c / Q * 100:.1f}%)")


if __name__ == "__main__":
    main()
