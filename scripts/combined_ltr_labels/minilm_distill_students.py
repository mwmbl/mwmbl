"""Cheap students distilled from the served MiniLM judge (`minilm-both-v1`).

Each student learns logit(teacher score) from what it can see of a (query, url) pair, and
scores every LLM-dataset, serving-pool and extension pair. The students train only on
pairs whose query isn't one of the 424 `llm_eval_queries`, so every eval query is out of
sample for them, as it is for the teacher, and `minilm_distill_experiment.py` can
cross-validate the LTR over those queries exactly as `minilm_experiment.py` does.

- `lr-feats`: ridge on the 52 LTR features. The LTR already sees these features, so this
  student can add nothing it couldn't learn itself. It's here to show that.
- `lr-docprior`: ridge on hashed unigrams and bigrams of the document text alone. It is
  query-independent, so it could be computed at index time.
- `lr-text`: `lr-docprior`'s features plus hashed query-term x document-term crosses.
- `static`: model2vec `potion-base-8M` static embeddings. A small MLP on the query and
  document vectors, their product and their cosine.

Writes `devdata/combined_ltr_labels/distill/<student>.json` ("query\\turl" -> score) and
`distill/students.json` (fidelity and timing), and `distill/pairs.parquet`, the pair table
the Modal students read.

    PYTHONPATH=. uv run --with model2vec python scripts/combined_ltr_labels/minilm_distill_students.py
"""

import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from model2vec import StaticModel
from scipy.stats import spearmanr
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import Ridge
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from mwmbl.tinysearchengine.super_search_select.judge import doc_text
from scripts.combined_ltr_labels.minilm_experiment import normalize
from scripts.combined_ltr_labels.objective_experiment import features, load

LABELS = Path("devdata/combined_ltr_labels")
OUT = LABELS / "distill"
TEACHER = "minilm-both-v1"
MANIFEST = Path("devdata/judge_train/eval_manifest.json")
STATIC_MODEL = "minishlab/potion-base-8M"
HASH_FEATURES = 2**20
QUERY_TERMS = 6
DOC_CROSS_TERMS = 40
VALIDATION_SHARE = 0.1
TIMING_QUERIES = 200
TOKEN = re.compile(r"\w+")


def teacher_scores() -> dict[str, float]:
    scores = json.loads((LABELS / "minilm_scores_ext.json").read_text())
    scores.update(json.loads((LABELS / "minilm_scores.json").read_text())[TEACHER])
    return scores


def pair_table() -> tuple[pd.DataFrame, np.ndarray]:
    """One row per distinct (query, url) over the LLM, serving and extension rows, with its
    doc text, teacher score and whether a student may train on it, plus its LTR features."""
    llm, ext, _, serving = load()
    frames = [llm, serving, ext]
    columns = ["query", "url", "title", "extract", "score", "staan_asked", "staan_rank", "from_staan"]
    table = pd.concat([frame[columns] for frame in frames], ignore_index=True)
    table["key"] = table["query"] + "\t" + table["url"]
    table = table.drop_duplicates("key").reset_index(drop=True)
    table["text"] = [
        doc_text(title if isinstance(title, str) else "", extract if isinstance(extract, str) else "")
        for title, extract in zip(table["title"], table["extract"])
    ]
    scores = teacher_scores()
    table["teacher"] = table["key"].map(scores)
    assert table["teacher"].notna().all(), "unscored pairs: run minilm_scores.py and modal_minilm_score.py"
    eval_queries = {normalize(q) for q in json.loads(MANIFEST.read_text())["llm_eval_queries"]}
    table["eval"] = table["query"].map(normalize).isin(eval_queries)
    return table, features(table)


def logit(p: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(clipped / (1 - clipped))


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def crosses(query: str, text: str) -> list[str]:
    doc_terms = dict.fromkeys(tokens(text)[:DOC_CROSS_TERMS])
    return [f"{q}|{d}" for q in tokens(query)[:QUERY_TERMS] for d in doc_terms]


class LinearFeatures:
    def __init__(self, table: pd.DataFrame, ltr_feats: np.ndarray):
        self.ltr_feats = ltr_feats
        self.doc_hasher = HashingVectorizer(n_features=HASH_FEATURES, ngram_range=(1, 2), dtype=np.float32)
        self.cross_hasher = HashingVectorizer(
            n_features=HASH_FEATURES, analyzer=lambda pair: crosses(*pair), dtype=np.float32
        )
        self.table = table

    def build(self, name: str, rows: np.ndarray):
        table = self.table.iloc[rows]
        if name == "lr-feats":
            return self.ltr_feats[rows]
        docs = self.doc_hasher.transform(table["text"])
        if name == "lr-docprior":
            return docs
        pairs = list(zip(table["query"], table["text"]))
        return sp.hstack([docs, self.cross_hasher.transform(pairs)]).tocsr()


class StaticFeatures:
    def __init__(self):
        self.model = StaticModel.from_pretrained(STATIC_MODEL)

    def build(self, queries: list[str], texts: list[str]) -> np.ndarray:
        distinct = list(dict.fromkeys(queries))
        query_vectors = dict(zip(distinct, self.model.encode(distinct)))
        q = np.stack([query_vectors[query] for query in queries])
        d = self.model.encode(texts)
        q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-9
        d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-9
        cosine = np.sum(q * d, axis=1, keepdims=True)
        return np.concatenate([q, d, q * d, cosine], axis=1).astype(np.float32)


def per_query_spearman(table: pd.DataFrame, predicted: np.ndarray, rows: np.ndarray) -> float:
    frame = pd.DataFrame(
        {"query": table["query"].to_numpy()[rows], "teacher": table["teacher"].to_numpy()[rows], "pred": predicted}
    )
    values = [
        spearmanr(group["teacher"], group["pred"]).statistic for _, group in frame.groupby("query") if len(group) > 2
    ]
    return float(np.nanmean(values))


def run():
    OUT.mkdir(exist_ok=True)
    table, ltr_feats = pair_table()
    table[["query", "url", "key", "text", "teacher", "eval"]].to_parquet(OUT / "pairs.parquet")
    target = logit(table["teacher"].to_numpy())
    train_queries = np.array(sorted(table.loc[~table["eval"], "query"].unique()))
    np.random.default_rng(0).shuffle(train_queries)
    validation_queries = set(train_queries[: int(len(train_queries) * VALIDATION_SHARE)])
    is_validation = table["query"].isin(validation_queries).to_numpy()
    fit_rows = np.flatnonzero(~table["eval"].to_numpy() & ~is_validation)
    validation_rows = np.flatnonzero(is_validation)
    all_train_rows = np.flatnonzero(~table["eval"].to_numpy())
    eval_rows = np.flatnonzero(table["eval"].to_numpy())
    print(f"{len(table)} pairs: {len(all_train_rows)} student-train, {len(eval_rows)} eval-query", flush=True)

    timing_queries = set(table.loc[table["eval"], "query"].drop_duplicates().iloc[:TIMING_QUERIES])
    timing_rows = np.flatnonzero(table["query"].isin(timing_queries).to_numpy())
    report = {}

    linear = LinearFeatures(table, ltr_feats)
    for name, alphas in [("lr-feats", [0.1, 10, 1000]), ("lr-docprior", [1, 10, 100]), ("lr-text", [1, 10, 100])]:
        fit_x, validation_x = linear.build(name, fit_rows), linear.build(name, validation_rows)
        scaler = StandardScaler().fit(fit_x) if name == "lr-feats" else None

        def scale(x, scaler=scaler):
            return scaler.transform(x) if scaler else x

        solver = "auto" if name == "lr-feats" else "sparse_cg"
        fidelity = {}
        for alpha in alphas:
            model = Ridge(alpha=alpha, solver=solver).fit(scale(fit_x), target[fit_rows])
            fidelity[alpha] = per_query_spearman(table, model.predict(scale(validation_x)), validation_rows)
        alpha = max(fidelity, key=fidelity.get)
        train_x = linear.build(name, all_train_rows)
        scaler = StandardScaler().fit(train_x) if name == "lr-feats" else None
        model = Ridge(alpha=alpha, solver=solver).fit(scale(train_x, scaler), target[all_train_rows])
        predicted = model.predict(scale(linear.build(name, np.arange(len(table))), scaler))
        started = time.perf_counter()
        model.predict(scale(linear.build(name, timing_rows), scaler))
        micros = (time.perf_counter() - started) / len(timing_rows) * 1e6
        report[name] = finish(table, name, predicted, eval_rows, micros, {"alpha": alpha, "validation": fidelity})

    static = StaticFeatures()
    static_x = static.build(table["query"].tolist(), table["text"].tolist())
    mlp = MLPRegressor(hidden_layer_sizes=(256,), early_stopping=True, max_iter=50, random_state=0)
    mlp.fit(static_x[all_train_rows], target[all_train_rows])
    predicted = mlp.predict(static_x)
    timing_table = table.iloc[timing_rows]
    started = time.perf_counter()
    mlp.predict(static.build(timing_table["query"].tolist(), timing_table["text"].tolist()))
    micros = (time.perf_counter() - started) / len(timing_rows) * 1e6
    report["static"] = finish(table, "static", predicted, eval_rows, micros, {"iterations": mlp.n_iter_})

    (OUT / "students.json").write_text(json.dumps(report, indent=2))


def finish(table, name, predicted, eval_rows, micros, extra) -> dict:
    fidelity = per_query_spearman(table, predicted[eval_rows], eval_rows)
    (OUT / f"{name}.json").write_text(json.dumps(dict(zip(table["key"], predicted.astype(float)))))
    print(f"{name}: per-query Spearman vs teacher {fidelity:.3f} on eval queries, {micros:.1f} µs/pair", flush=True)
    return {"fidelity": fidelity, "micros_per_pair": micros, **extra}


if __name__ == "__main__":
    run()
