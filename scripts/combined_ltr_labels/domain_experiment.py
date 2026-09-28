"""Domain-quality features for the rank:ndcg+new Combined Search model, under the objective
experiment's 5-fold cross-validation over the 849 LLM-labelled queries.

Each arm adds one set of columns to `ndcg+new`'s features:

- `serp`: `curated` and the SERP counts of `domain_features.py`;
- `raw`: those plus the host's shape;
- `raw+crawl`: those plus the crawl's page and inlink counts (every `domain_features` column);
- `hq`: a host-quality model's predicted `ethos` (0-3), from every `domain_features` column
  plus `domain_score`. It is an XGBoost regressor on the training folds' LLM and serving-pool
  rows, so it can score a host no label has seen;
- `te`: target encoding. The smoothed mean `ethos` and `overall` of the host's and the
  registered domain's labelled rows, and how many there are. It remembers hosts but can't
  generalise;
- `all`: `raw+crawl`, `hq` and `te` together.

`hq` and `te` are learned from labels, so they are cross-fitted: a training row's values come
from the other inner folds of its outer training set, and a test row's from the whole outer
training set. The extension rows have no `ethos` and take the whole outer training set's.

With `--minilm`, the arms instead add the domain features beside the served MiniLM judge
(`both`), cross-validated over the 424 queries no judge saw, as `minilm_experiment.py`'s
`all` setting.

Writes `devdata/combined_ltr_labels/domain_experiment[_minilm].json` with per-query scores.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from scripts.combined_ltr_labels.domain_features import (
    ALL_NAMES,
    RAW_NAMES,
    SERP_NAMES,
    apex_of,
    domain_columns,
    host_of,
)
from scripts.combined_ltr_labels.minilm_experiment import MANIFEST, SCORES, minilm_columns, normalize
from scripts.combined_ltr_labels.objective_experiment import (
    FEATURE_NAMES,
    FOLDS,
    load,
    read_jsonl,
    score_population,
    train,
)
from scripts.combined_ltr_labels.objective_experiment import features as base_features

LABELS = Path("devdata/combined_ltr_labels")
OBJECTIVE = "ndcg"
INNER_FOLDS = 5
TE_PRIOR = 5
TE_NAMES = ["te_host_ethos", "te_host_overall", "te_host_rows", "te_apex_ethos", "te_apex_overall", "te_apex_rows"]
HQ_PARAMS = {"objective": "reg:squarederror", "eta": 0.1, "max_depth": 4, "lambda": 2.0, "nthread": 4}
HQ_ROUNDS = 200
DOMAIN_SCORE = FEATURE_NAMES.index("domain_score")

ARMS = {
    "base": [],
    "serp": ["serp"],
    "raw": ["raw"],
    "raw+crawl": ["raw+crawl"],
    "hq": ["hq"],
    "te": ["te"],
    "all": ["raw+crawl", "hq", "te"],
}
MINILM_ARMS = {
    "base": [],
    "both": ["minilm"],
    "raw+crawl": ["raw+crawl"],
    "hq": ["hq"],
    "both+raw+crawl": ["minilm", "raw+crawl"],
    "both+hq": ["minilm", "hq"],
    "both+all": ["minilm", "raw+crawl", "hq", "te"],
}
STATIC_COLUMNS = {
    "serp": [ALL_NAMES.index(name) for name in SERP_NAMES],
    "raw": [ALL_NAMES.index(name) for name in RAW_NAMES],
    "raw+crawl": list(range(len(ALL_NAMES))),
}


def with_ethos(llm: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    ethos = {(row["query"], row["url"]): row["ethos"] for row in read_jsonl(LABELS / "pass3_serving_pool.jsonl")}
    new = new.copy()
    new["ethos"] = [ethos[pair] for pair in zip(new["query"], new["url"])]
    return new


class Learned:
    """The label-derived domain columns (`hq`, `te`), fitted on one set of labelled rows."""

    def __init__(self, labelled: pd.DataFrame, domain: np.ndarray):
        self.te = {
            level: labelled.assign(key=keys)
            .groupby("key")
            .agg(ethos_sum=("ethos", "sum"), overall_sum=("overall", "sum"), rows=("ethos", "size"))
            for level, keys in [("host", labelled["host"]), ("apex", labelled["apex"])]
        }
        self.mean_ethos = labelled["ethos"].mean()
        self.mean_overall = labelled["overall"].mean()
        dmatrix = xgb.DMatrix(domain, label=labelled["ethos"].to_numpy())
        self.hq = xgb.train(HQ_PARAMS, dmatrix, HQ_ROUNDS)

    def columns(self, frame: pd.DataFrame, domain: np.ndarray) -> dict[str, np.ndarray]:
        te = []
        for level in ("host", "apex"):
            stats = self.te[level].reindex(frame[level].to_numpy()).fillna(0.0)
            rows = stats["rows"].to_numpy()
            te += [
                (stats["ethos_sum"].to_numpy() + TE_PRIOR * self.mean_ethos) / (rows + TE_PRIOR),
                (stats["overall_sum"].to_numpy() + TE_PRIOR * self.mean_overall) / (rows + TE_PRIOR),
                rows,
            ]
        return {
            "te": np.stack(te, axis=1).astype(np.float32),
            "hq": self.hq.predict(xgb.DMatrix(domain))[:, None].astype(np.float32),
        }


def hq_inputs(domain: np.ndarray, base: np.ndarray) -> np.ndarray:
    return np.concatenate([domain, base[:, [DOMAIN_SCORE]]], axis=1)


def learned_columns(
    labelled: pd.DataFrame, labelled_hq: np.ndarray, others: list[tuple[pd.DataFrame, np.ndarray]]
) -> tuple[dict[str, np.ndarray], list[dict[str, np.ndarray]]]:
    """Cross-fitted `hq` and `te` for the labelled training rows, and whole-set values for
    each (frame, hq inputs) in `others`."""
    queries = np.array(sorted(labelled["query"].unique()))
    np.random.default_rng(1).shuffle(queries)
    inner = np.array_split(queries, INNER_FOLDS)
    fitted = {
        "te": np.zeros((len(labelled), len(TE_NAMES)), np.float32),
        "hq": np.zeros((len(labelled), 1), np.float32),
    }
    for fold_queries in inner:
        held = labelled["query"].isin(set(fold_queries)).to_numpy()
        model = Learned(labelled[~held], labelled_hq[~held])
        for name, values in model.columns(labelled[held], labelled_hq[held]).items():
            fitted[name][held] = values
    whole = Learned(labelled, labelled_hq)
    return fitted, [whole.columns(frame, hq) for frame, hq in others]


def assemble(
    base: np.ndarray, static: np.ndarray, learned: dict[str, np.ndarray], judge: np.ndarray | None, groups: list[str]
) -> np.ndarray:
    parts = [base]
    for group in groups:
        if group in STATIC_COLUMNS:
            parts.append(static[:, STATIC_COLUMNS[group]])
        elif group == "minilm":
            parts.append(judge)
        else:
            parts.append(learned[group])
    return np.concatenate(parts, axis=1)


def run(minilm: bool):
    llm, ext, new, serving = load()
    new = with_ethos(llm, new)
    frames = {"llm": llm, "ext": ext, "new": new, "serving": serving}
    for frame in frames.values():
        frame["host"] = frame["url"].map(host_of)
        frame["apex"] = frame["host"].map(apex_of)
    base = {name: base_features(frame) for name, frame in frames.items()}
    static = {name: domain_columns(frame) for name, frame in frames.items()}
    hq_in = {name: hq_inputs(static[name], base[name]) for name in frames}
    print("features built", flush=True)

    if minilm:
        scores = json.loads(SCORES.read_text())
        both = list(scores).index("minilm-both-v1")
        judge = {name: minilm_columns(frame, scores)[:, [both]] for name, frame in frames.items()}
        eval_queries = {normalize(q) for q in json.loads(MANIFEST.read_text())["llm_eval_queries"]}
        test_pool = np.array(sorted(q for q in llm["query"].unique() if normalize(q) in eval_queries))
        arms, out = MINILM_ARMS, LABELS / "domain_experiment_minilm.json"
    else:
        judge = {name: None for name in frames}
        test_pool = np.array(sorted(llm["query"].unique()))
        arms, out = ARMS, LABELS / "domain_experiment.json"
    np.random.default_rng(0).shuffle(test_pool)
    folds = np.array_split(test_pool, FOLDS)
    results = {arm: {"original": {}, "serving": {}} for arm in arms}

    for fold, test_queries in enumerate(folds):
        test = set(test_queries)
        test_norm = {normalize(q) for q in test}
        llm_test = llm["query"].isin(test).to_numpy()
        serving_test = serving["query"].isin(test).to_numpy()
        new_train = ~new["query"].isin(test).to_numpy()
        ext_train = ~ext["query"].map(normalize).isin(test_norm).to_numpy()

        labelled = pd.concat([llm[~llm_test], new[new_train]], ignore_index=True)
        labelled_hq = np.concatenate([hq_in["llm"][~llm_test], hq_in["new"][new_train]])
        fitted, (ext_learned, llm_learned, serving_learned) = learned_columns(
            labelled,
            labelled_hq,
            [
                (ext[ext_train], hq_in["ext"][ext_train]),
                (llm[llm_test], hq_in["llm"][llm_test]),
                (serving[serving_test], hq_in["serving"][serving_test]),
            ],
        )
        num_llm_train = int((~llm_test).sum())
        train_frame = pd.concat([llm[~llm_test], ext[ext_train], new[new_train]], ignore_index=True)
        train_parts = {
            "base": np.concatenate([base["llm"][~llm_test], base["ext"][ext_train], base["new"][new_train]]),
            "static": np.concatenate([static["llm"][~llm_test], static["ext"][ext_train], static["new"][new_train]]),
            "learned": {
                name: np.concatenate([fitted[name][:num_llm_train], ext_learned[name], fitted[name][num_llm_train:]])
                for name in fitted
            },
            "judge": None
            if not minilm
            else np.concatenate([judge["llm"][~llm_test], judge["ext"][ext_train], judge["new"][new_train]]),
        }
        for arm, groups in arms.items():
            train_feats = assemble(
                train_parts["base"], train_parts["static"], train_parts["learned"], train_parts["judge"], groups
            )
            booster = train(OBJECTIVE, train_frame, train_feats)
            for population, name, mask, learned in [
                ("original", "llm", llm_test, llm_learned),
                ("serving", "serving", serving_test, serving_learned),
            ]:
                test_judge = None if judge[name] is None else judge[name][mask]
                test_feats = assemble(base[name][mask], static[name][mask], learned, test_judge, groups)
                results[arm][population].update(
                    score_population(booster, frames[name][mask].reset_index(drop=True), test_feats)
                )
            print(f"fold {fold} {arm} done", flush=True)

    out.write_text(json.dumps(results))
    report(results)


def report(results: dict[str, dict[str, dict]]):
    rng = np.random.default_rng(0)
    for population in ("serving", "original"):
        print(f"\n## {population} pool\n")
        print("| Arm | NDCG@10 | vs base, 95% CI | weak in top 10 | index in top 10 |")
        print("|---|---|---|---|---|")
        queries = sorted(q for q, v in results["base"][population].items() if not np.isnan(v["ndcg@10"]))
        baseline = np.array([results["base"][population][q]["ndcg@10"] for q in queries])
        for arm, scores in results.items():
            values = np.array([scores[population][q]["ndcg@10"] for q in queries])
            diff = values - baseline
            means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
            weak = np.nanmean([scores[population][q]["weak_in_top10"] for q in queries])
            index = np.nanmean([scores[population][q]["index_in_top10"] for q in queries])
            print(
                f"| {arm} | {values.mean():.4f} | {diff.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, "
                f"{np.percentile(means, 97.5):+.4f}] | {weak:.1%} | {index:.1%} |"
            )


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        report(json.loads((LABELS / "domain_experiment.json").read_text()))
    else:
        run(minilm="--minilm" in sys.argv)
