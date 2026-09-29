"""Scores the extension dataset's 252k pairs with the served MiniLM judge on Modal.

The distillation students need far more teacher labels than `minilm_scores.json` holds, and
on CPU these pairs take about an hour. This loads the torch checkpoint of `minilm-both-v1`
from the `judge-train` volume and returns sigmoid(logit), as `Judge.score` does.

It also rescores 2,000 pairs already in `minilm_scores.json` and refuses to write anything
unless their Spearman against the ONNX scores is at least 0.999.

Writes `devdata/combined_ltr_labels/minilm_scores_ext.json`: "query\\turl" -> score.

    uv run --with modal modal run scripts/combined_ltr_labels/modal_minilm_score.py
"""

import json
from pathlib import Path

import modal

MODEL = "minilm-both-v1"
EXT_DATASET = Path("devdata/rankeval-2026-04/learning-to-rank.csv.gz")
SCORES = Path("devdata/combined_ltr_labels/minilm_scores.json")
OUT = Path("devdata/combined_ltr_labels/minilm_scores_ext.json")
MAX_LENGTH = 256
CHUNK = 20_000
PARITY_PAIRS = 2_000
MIN_PARITY_SPEARMAN = 0.999

app = modal.App("minilm-score")
image = modal.Image.debian_slim(python_version="3.11").pip_install("torch", "sentence-transformers>=4.1")
volume = modal.Volume.from_name("judge-train")


@app.function(image=image, gpu="T4", timeout=3600, volumes={"/ckpt": volume})
def score(pairs: list[tuple[str, str]]) -> list[float]:
    import torch
    from sentence_transformers.cross_encoder import CrossEncoder

    model = CrossEncoder(f"/ckpt/{MODEL}/final", max_length=MAX_LENGTH)
    # The saved checkpoint's default activation is the identity, so it would return logits.
    scores = model.predict(pairs, batch_size=256, activation_fn=torch.nn.Sigmoid(), show_progress_bar=False)
    return [float(s) for s in scores]


@app.local_entrypoint()
def main():
    import numpy as np
    import pandas as pd
    from scipy.stats import spearmanr

    from mwmbl.tinysearchengine.super_search_select.judge import doc_text
    from scripts.combined_ltr_labels.minilm_scores import pairs as scored_pairs

    ext = pd.read_csv(EXT_DATASET, lineterminator="\n")
    keys = [f"{query}\t{url}" for query, url in zip(ext["query"], ext["url"])]
    ext_pairs = [
        (query, doc_text(title if isinstance(title, str) else "", extract if isinstance(extract, str) else ""))
        for query, title, extract in zip(ext["query"], ext["title"], ext["extract"])
    ]

    onnx_scores = json.loads(SCORES.read_text())[MODEL]
    known = [(query, url, text) for query, docs in scored_pairs().items() for url, text in docs]
    sample = [known[i] for i in np.random.default_rng(0).choice(len(known), PARITY_PAIRS, replace=False)]
    parity_pairs = [(query, text) for query, _, text in sample]

    chunks = [ext_pairs[start : start + CHUNK] for start in range(0, len(ext_pairs), CHUNK)]
    print(f"{len(ext_pairs)} pairs in {len(chunks)} chunks, plus {PARITY_PAIRS} parity pairs", flush=True)
    results = list(score.map([parity_pairs, *chunks]))

    torch_parity = results[0]
    reference = [onnx_scores[f"{query}\t{url}"] for query, url, _ in sample]
    parity = spearmanr(torch_parity, reference).statistic
    max_diff = float(np.max(np.abs(np.array(torch_parity) - np.array(reference))))
    print(f"parity vs ONNX: Spearman {parity:.6f}, max |diff| {max_diff:.5f}")
    assert parity >= MIN_PARITY_SPEARMAN, "torch scores don't match the ONNX judge"
    assert max_diff < 0.01, "torch scores aren't on the ONNX judge's scale"

    ext_scores = [value for chunk in results[1:] for value in chunk]
    OUT.write_text(json.dumps(dict(zip(keys, ext_scores))))
    print(f"wrote {len(ext_scores)} scores to {OUT}")
