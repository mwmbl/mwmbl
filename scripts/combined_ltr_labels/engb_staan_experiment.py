"""Retrain the Combined Search model on the training queries' en-gb Staan results.

`engb_staan.py` fetched and judged Staan's en-gb results for the 849 training queries. This
swaps them in for the en-us results the training data was built with (`load_engb`), then:

    cv       5-fold cross-validation on the en-gb serving pool: en-gb Staan plus the index's
             top 30. Compares the en-us-trained `ndcg+new`, the en-gb-trained one, and
             Staan-first filled by each.
    rank     end to end on the en-gb eval, as `engb_eval.py` -> engb_staan_arms.json
    batches / consolidate / report   as `engb_eval.py`

The swap, per query:

- A row only en-us Staan pooled is dropped: it isn't in the en-gb pool at all.
- A row en-us Staan pooled first but another pool also found keeps its label but loses its
  Staan rank, and is dropped too, since its `score` is Staan's and the other pool's is lost.
- A row in en-gb Staan's results gets its en-gb rank and `staan_score(rank)`, whoever
  pooled it; an index row of the serving pool it duplicates is dropped.
- An en-gb Staan result no pool had carries its `pass3_staan_engb.jsonl` grade, moved back by
  its judge's shrunk mean anchor drift.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import sys
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import pandas as pd
import xgboost as xgb
from django.conf import settings

from mwmbl.rankeval.evaluation.haiku_arms_report import staan_first
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE
from scripts.combined_ltr_labels.engb_eval import (
    ENGB,
    SHIPPED,
    BoosterModel,
    Run,
    batches,
    consolidate,
    rank_all,
    report,
)
from scripts.combined_ltr_labels.objective_experiment import (
    DRIFT_SHRINKAGE,
    FOLDS,
    features,
    kept,
    load,
    read_jsonl,
    train,
)

LABELS = Path("devdata/combined_ltr_labels")
STAAN_ENGB = LABELS / "pass2_staan_engb.jsonl"
STAAN_ENGB_JUDGMENTS = LABELS / "pass3_staan_engb.jsonl"
CV_OUT = LABELS / "engb_staan_experiment.json"
STAAN_RUN = Run(
    arms=LABELS / "engb_staan_arms.json",
    judgments=LABELS / "pass3_engb_staan.jsonl",
    work=LABELS / "engb_staan_work",
    baselines=("ndcg+new", "staan-first, fill ndcg+new"),
)
FILL = "staan-first, fill"


def staan_engb_grades() -> dict[tuple[str, str], int]:
    rows = read_jsonl(STAAN_ENGB_JUDGMENTS)
    original = {
        (j["query"], j["url"]): j["overall"] for j in read_jsonl(Path("devdata/llm_relabel/pass3_judgments.jsonl"))
    }
    drifts: dict[str, list[int]] = {}
    for row in rows:
        if row.get("anchor"):
            drifts.setdefault(row["judge"], []).append(row["overall"] - original[(row["query"], row["url"])])
    drift = {judge: sum(values) / (len(values) + DRIFT_SHRINKAGE) for judge, values in drifts.items()}
    return {
        (row["query"], row["url"]): int(np.clip(round(row["overall"] - drift.get(row["judge"], 0.0)), 0, 10))
        for row in rows
        if not row.get("anchor")
    }


def load_engb() -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame
]:
    """The en-us `load()` frames and their en-gb Staan counterparts: llm, ext, new, serving,
    llm_gb, new_gb, serving_gb (ext is shared, so returned once)."""
    llm, ext, new, serving = load()
    gb = {record["query"]: record["results"] for record in read_jsonl(STAAN_ENGB)}
    gb_rank = {(q, r["url"]): r["rank"] for q, results in gb.items() for r in results}
    grades = dict(zip(zip(serving["query"], serving["url"]), serving["overall"]))
    grades.update(zip(zip(llm["query"], llm["url"]), llm["overall"]))
    for pair, grade in staan_engb_grades().items():
        grades.setdefault(pair, grade)

    def as_gb_staan(frame: pd.DataFrame) -> pd.DataFrame:
        pairs = list(zip(frame["query"], frame["url"]))
        frame = frame.copy()
        ranks = [gb_rank.get(pair) for pair in pairs]
        in_gb = np.array([rank is not None for rank in ranks])
        frame["from_staan"] = in_gb
        frame["staan_rank"] = pd.Series(ranks, index=frame.index, dtype=object)
        frame.loc[in_gb, "score"] = [STAAN_TOP_SCORE - rank for rank in ranks if rank is not None]
        return frame

    us_staan_first = llm["pools"].str.startswith("staan").to_numpy() & llm["from_staan"].to_numpy()
    llm_gb = as_gb_staan(llm)
    llm_gb = llm_gb[~(us_staan_first & ~llm_gb["from_staan"].to_numpy())]
    present = set(zip(llm_gb["query"], llm_gb["url"]))
    added = [
        {
            "query": query,
            "url": r["url"],
            "title": r["title"] or "",
            "extract": r["extract"] or "",
            "score": STAAN_TOP_SCORE - r["rank"],
            "staan_asked": True,
            "staan_rank": r["rank"],
            "from_staan": True,
            "overall": grades[(query, r["url"])],
            "pools": "staan-gb",
            "source": "llm",
        }
        for query, results in gb.items()
        for r in results
        if (query, r["url"]) not in present
    ]
    llm_gb = pd.concat([llm_gb, pd.DataFrame(added)], ignore_index=True)

    new_gb = new[[pair not in gb_rank for pair in zip(new["query"], new["url"])]].copy()

    # The pool's `both` rows are Staan's in `serving`; the index had them too, so they stay.
    pool = json.loads((LABELS / "pool.json").read_text())
    index_pairs = {(q, row["url"]) for q, rows in pool.items() for row in rows if row["source"] in ("index", "both")}
    index_rows = serving[[pair in index_pairs for pair in zip(serving["query"], serving["url"])]].copy()
    index_rows = index_rows[[pair not in gb_rank for pair in zip(index_rows["query"], index_rows["url"])]]
    index_rows["from_staan"] = False
    index_rows["staan_rank"] = None
    staan_rows = pd.DataFrame(
        [
            {
                "query": query,
                "url": r["url"],
                "title": r["title"] or "",
                "extract": r["extract"] or "",
                "score": STAAN_TOP_SCORE - r["rank"],
                "staan_asked": True,
                "staan_rank": r["rank"],
                "from_staan": True,
                "overall": grades[(query, r["url"])],
                "new": False,
            }
            for query, results in gb.items()
            for r in results
        ]
    )
    serving_gb = pd.concat([index_rows, staan_rows], ignore_index=True)
    serving_gb["staan_rank"] = serving_gb["staan_rank"].astype(object)
    print(
        f"llm {len(llm)} -> {len(llm_gb)} rows ({len(added)} en-gb Staan rows added); "
        f"new {len(new)} -> {len(new_gb)}; serving {len(serving)} -> {len(serving_gb)}"
    )
    return llm, ext, new, serving, llm_gb, new_gb, serving_gb


def ranked_lists(booster: xgb.Booster, frame: pd.DataFrame, feats: np.ndarray) -> dict[str, dict[str, np.ndarray]]:
    """Per query: the gains, the model's order (the filter as an exclusion), and Staan-first."""
    scores = booster.predict(xgb.DMatrix(feats))
    keep = kept(frame, feats)
    gains = frame["overall"].to_numpy().astype(float)
    from_staan = frame["from_staan"].to_numpy()
    staan_rank = frame["staan_rank"].to_numpy()
    out = {}
    for query, index in frame.groupby("query").indices.items():
        k = index[keep[index]]
        order = k[np.argsort(-scores[k], kind="stable")]
        staan = index[from_staan[index]]
        staan = staan[np.argsort(staan_rank[staan].astype(float), kind="stable")]
        fill = np.array(list(staan) + [i for i in order if not from_staan[i]], dtype=int)
        out[query] = {"gains": gains[index], "model": gains[order], "staan-first": gains[fill], "order": order}
    return out


def cv():
    llm, ext, new, serving, llm_gb, new_gb, serving_gb = load_engb()
    frames = {"llm": llm, "ext": ext, "new": new, "llm_gb": llm_gb, "new_gb": new_gb, "serving_gb": serving_gb}
    feats = {name: features(frame) for name, frame in frames.items()}
    queries = np.array(sorted(llm["query"].unique()))
    np.random.default_rng(0).shuffle(queries)
    results: dict[str, dict[str, float]] = {}
    swaps: dict[str, list[float]] = {}
    for fold, test_queries in enumerate(np.array_split(queries, FOLDS)):
        test = set(test_queries)
        test_norm = {q.lower().strip() for q in test}
        ext_mask = ~ext["query"].str.lower().str.strip().isin(test_norm)
        serving_mask = serving_gb["query"].isin(test).to_numpy()
        held_out = serving_gb[serving_mask].reset_index(drop=True)
        held_feats = feats["serving_gb"][serving_mask]
        for arm, names in {"en-us": ["llm", "ext", "new"], "en-gb": ["llm_gb", "ext", "new_gb"]}.items():
            masks = [ext_mask if n == "ext" else ~frames[n]["query"].isin(test) for n in names]
            frame = pd.concat([frames[n][m] for n, m in zip(names, masks)], ignore_index=True)
            booster = train("ndcg", frame, np.concatenate([feats[n][m.to_numpy()] for n, m in zip(names, masks)]))
            for query, lists in ranked_lists(booster, held_out, held_feats).items():
                results.setdefault(f"ndcg+new ({arm})", {})[query] = _ndcg(lists["gains"], lists["model"])
                results.setdefault(f"{FILL} ndcg+new ({arm})", {})[query] = _ndcg(lists["gains"], lists["staan-first"])
                top = lists["order"][:10]
                swaps.setdefault(arm, []).extend(
                    held_out["overall"].to_numpy()[top][~held_out["from_staan"].to_numpy()[top]]
                )
            print(f"fold {fold} {arm}", flush=True)
    CV_OUT.write_text(json.dumps(results))
    for arm, values in swaps.items():
        print(
            f"{arm}: index pages in the top ten {len(values) / len(results['ndcg+new (en-us)']):.2f} a query, "
            f"mean grade {np.mean(values):.2f}"
        )
    cv_report(results)


def _ndcg(gains: np.ndarray, ranked: np.ndarray) -> float:
    discounts = 1 / np.log2(np.arange(2, 12))
    ideal = np.sort(gains)[::-1][:10]
    top = ranked[:10]
    return float(np.sum(top * discounts[: len(top)]) / np.sum(ideal * discounts[: len(ideal)]))


def cv_report(results: dict[str, dict[str, float]] | None = None):
    results = results or json.loads(CV_OUT.read_text())
    base = "ndcg+new (en-us)"
    queries = sorted(q for q, v in results[base].items() if not np.isnan(v))
    baseline = np.array([results[base][q] for q in queries])
    rng = np.random.default_rng(0)
    print(f"\n## en-gb serving pool, 5-fold CV ({len(queries)} queries)\n")
    print("| Arm | NDCG@10 | vs ndcg+new (en-us) |\n|---|---|---|")
    for arm, scores in results.items():
        values = np.array([scores[q] for q in queries])
        diff = values - baseline
        means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
        print(
            f"| {arm} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
            f"{np.percentile(means, 97.5):+.4f}] |"
        )


def rank():
    llm, ext, new, _, llm_gb, new_gb, _ = load_engb()
    models = {SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))}
    for arm, frames in {"ndcg+new": [llm, ext, new], "ndcg+new (en-gb Staan)": [llm_gb, ext, new_gb]}.items():
        frame = pd.concat(frames, ignore_index=True)
        models[arm] = BoosterModel(train("ndcg", frame, np.concatenate([features(f) for f in frames])))
        print("trained", arm, flush=True)
    rank_all(models, STAAN_RUN.arms, keep=30, no_mmr=tuple(models))
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    arms = json.loads(STAAN_RUN.arms.read_text())
    for query, entry in arms.items():
        staan = rows[query]["lists"]["staan"][:10]
        lists = entry["lists"]
        for arm in ("ndcg+new", "ndcg+new (en-gb Staan)"):
            for variant in (arm, f"{arm}, no MMR"):
                lists[f"{FILL} {variant}"] = staan_first(staan, lists[variant])
        for arm in list(lists):
            lists[arm] = lists[arm][:10]
        shown = {url for urls in lists.values() for url in urls}
        entry["pages"] = {url: page for url, page in entry["pages"].items() if url in shown}
    STAAN_RUN.arms.write_text(json.dumps(arms))


if __name__ == "__main__":
    command = sys.argv[1]
    commands = {"cv": cv, "cv-report": cv_report, "rank": rank}
    if command in commands:
        commands[command]()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](STAAN_RUN)
