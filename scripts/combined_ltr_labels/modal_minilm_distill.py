"""Neural students distilled from the served MiniLM judge on Modal.

- `ce-l2`: a 2-layer cross-encoder (`cross-encoder/ms-marco-TinyBERT-L-2-v2`, max_length
  128), trained with MSE on the teacher's logits.
- `bi-enc`: a MiniLM-L6 bi-encoder (`sentence-transformers/all-MiniLM-L6-v2`), trained with
  Margin-MSE on the teacher's logit differences between two documents of one query. Its
  document vectors could be computed at index time, so serving costs one query encoding and
  a dot product per candidate.

Both train on the pairs `minilm_distill_students.py` marks as student-train (no eval
query) and score every pair in `distill/pairs.parquet`, which that script writes first.

Writes `devdata/combined_ltr_labels/distill/<student>.json` ("query\\turl" -> score) and the
student's ONNX export to `distill/models/<student>/`.

    uv run --with modal modal run scripts/combined_ltr_labels/modal_minilm_distill.py --student ce-l2
    uv run --with modal modal run scripts/combined_ltr_labels/modal_minilm_distill.py --student bi-enc
"""

import io
import json
import tarfile
from pathlib import Path

import modal

DISTILL = Path("devdata/combined_ltr_labels/distill")
PAIRS = DISTILL / "pairs.parquet"
BASES = {"ce-l2": "cross-encoder/ms-marco-TinyBERT-L-2-v2", "bi-enc": "sentence-transformers/all-MiniLM-L6-v2"}
MAX_LENGTH = 128
EPOCHS = 2
BATCH_SIZE = 64
TRIPLES_PER_QUERY = 32
VALIDATION_SHARE = 0.1

app = modal.App("minilm-distill")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch", "sentence-transformers>=4.1", "transformers", "accelerate", "datasets", "pandas", "pyarrow", "scipy"
    )
    .pip_install("optimum[onnxruntime]>=1.24")
    .add_local_file(PAIRS, "/data/pairs.parquet")
)


def logit(p):
    import numpy as np

    clipped = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(clipped / (1 - clipped))


def per_query_spearman(queries, teacher, predicted) -> float:
    import numpy as np
    import pandas as pd
    from scipy.stats import spearmanr

    frame = pd.DataFrame({"query": queries, "teacher": teacher, "pred": predicted})
    values = [spearmanr(g["teacher"], g["pred"]).statistic for _, g in frame.groupby("query") if len(g) > 2]
    return float(np.nanmean(values))


@app.function(image=image, gpu="T4", timeout=3 * 3600)
def train(student: str) -> tuple[list[float], dict, bytes]:
    import numpy as np
    import pandas as pd
    from datasets import Dataset
    from transformers import set_seed

    set_seed(0)
    pairs = pd.read_parquet("/data/pairs.parquet")
    pairs["target"] = logit(pairs["teacher"].to_numpy())
    train_queries = np.array(sorted(pairs.loc[~pairs["eval"], "query"].unique()))
    np.random.default_rng(0).shuffle(train_queries)
    validation_queries = set(train_queries[: int(len(train_queries) * VALIDATION_SHARE)])
    is_validation = pairs["query"].isin(validation_queries)
    fit = pairs[~pairs["eval"] & ~is_validation]
    validation = pairs[is_validation]
    model_dir = f"/tmp/{student}"
    onnx_dir = Path(f"/tmp/{student}-onnx")

    if student == "ce-l2":
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from sentence_transformers.cross_encoder import (
            CrossEncoder,
            CrossEncoderTrainer,
            CrossEncoderTrainingArguments,
        )
        from sentence_transformers.cross_encoder.losses import MSELoss

        model = CrossEncoder(BASES[student], num_labels=1, max_length=MAX_LENGTH)
        dataset = Dataset.from_dict({"query": fit["query"], "doc": fit["text"], "label": fit["target"]})
        args = CrossEncoderTrainingArguments(
            output_dir="/tmp/out",
            num_train_epochs=EPOCHS,
            per_device_train_batch_size=BATCH_SIZE,
            learning_rate=7e-5,
            warmup_ratio=0.1,
            fp16=True,
            save_strategy="no",
            report_to="none",
            seed=0,
        )
        CrossEncoderTrainer(model=model, args=args, train_dataset=dataset, loss=MSELoss(model)).train()

        def predict(frame):
            inputs = list(zip(frame["query"], frame["text"]))
            return model.predict(inputs, batch_size=512, activation_fn=lambda x: x, show_progress_bar=False)

        model.save_pretrained(model_dir)
        ORTModelForSequenceClassification.from_pretrained(model_dir, export=True).save_pretrained(onnx_dir)
        model.tokenizer.save_pretrained(str(onnx_dir))
    else:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from sentence_transformers import SentenceTransformer, SentenceTransformerTrainer
        from sentence_transformers.losses import MarginMSELoss
        from sentence_transformers.training_args import SentenceTransformerTrainingArguments

        model = SentenceTransformer(BASES[student])
        model.max_seq_length = MAX_LENGTH
        rng = np.random.default_rng(0)
        triples = {"query": [], "pos": [], "neg": [], "label": []}
        for query, group in fit.groupby("query"):
            if len(group) < 2:
                continue
            texts, targets = group["text"].to_numpy(), group["target"].to_numpy()
            for _ in range(TRIPLES_PER_QUERY):
                a, b = rng.choice(len(group), 2, replace=False)
                triples["query"].append(query)
                triples["pos"].append(texts[a])
                triples["neg"].append(texts[b])
                triples["label"].append(float(targets[a] - targets[b]))
        args = SentenceTransformerTrainingArguments(
            output_dir="/tmp/out",
            num_train_epochs=EPOCHS,
            per_device_train_batch_size=BATCH_SIZE,
            learning_rate=2e-5,
            warmup_ratio=0.1,
            fp16=True,
            save_strategy="no",
            report_to="none",
            seed=0,
        )
        SentenceTransformerTrainer(
            model=model, args=args, train_dataset=Dataset.from_dict(triples), loss=MarginMSELoss(model)
        ).train()

        def predict(frame):
            distinct = list(dict.fromkeys(frame["query"]))
            query_vectors = dict(zip(distinct, model.encode(distinct, batch_size=512)))
            doc_vectors = model.encode(frame["text"].tolist(), batch_size=512)
            return np.array([query_vectors[q] @ d for q, d in zip(frame["query"], doc_vectors)])

        model.save_pretrained(model_dir)
        ORTModelForFeatureExtraction.from_pretrained(model_dir, export=True).save_pretrained(onnx_dir)
        model.tokenizer.save_pretrained(str(onnx_dir))

    meta = {
        "student": student,
        "base": BASES[student],
        "fit_pairs": len(fit),
        "validation_fidelity": per_query_spearman(validation["query"], validation["teacher"], predict(validation)),
    }
    scores = predict(pairs)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(onnx_dir, arcname=".")
    return [float(s) for s in scores], meta, buffer.getvalue()


@app.local_entrypoint()
def main(student: str):
    import pandas as pd

    assert student in BASES, f"--student must be one of {sorted(BASES)}"
    pairs = pd.read_parquet(PAIRS)
    scores, meta, artifact = train.remote(student)
    eval_rows = pairs["eval"].to_numpy()
    meta["fidelity"] = per_query_spearman(
        pairs["query"][eval_rows], pairs["teacher"][eval_rows], pd.Series(scores)[eval_rows]
    )
    print(json.dumps(meta, indent=2))
    (DISTILL / f"{student}.json").write_text(json.dumps(dict(zip(pairs["key"], scores))))
    model_dir = DISTILL / "models" / student
    model_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as tar:
        tar.extractall(model_dir)
    (model_dir / "meta.json").write_text(json.dumps(meta, indent=2))
