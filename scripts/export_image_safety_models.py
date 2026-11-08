"""Build the two ONNX models that image search's safety pipeline runs.

Run it once per deployment; it needs torch, which the server does not:

    uv run --no-project --python 3.11 \\
        --with torch --with transformers --with onnx --with onnxscript --with onnxruntime \\
        --with huggingface_hub --with "ml_dtypes>=0.5" \\
        scripts/export_image_safety_models.py devdata/image_safety

It writes into the output directory:

- text/model.onnx and text/tokenizer.json: eliasalbouzidi/distilbert-nsfw-text-classifier
  (Apache-2.0), a DistilBERT classifier fine-tuned on safe/NSFW text.
- text/labels.json: the model's id -> label map, which the server reads to find the NSFW class.
- nudenet/320n.onnx: the YOLOv8 body-part detector from the NudeNet wheel (MIT).

Point IMAGE_SEARCH_MODEL_DIR at the output directory.
"""

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import torch
from huggingface_hub import hf_hub_download
from transformers import AutoModelForSequenceClassification, AutoTokenizer

TEXT_MODEL = "eliasalbouzidi/distilbert-nsfw-text-classifier"
NUDENET_WHEEL_INDEX = "https://pypi.org/pypi/nudenet/json"
NUDENET_MODEL_SUFFIX = "nudenet/320n.onnx"
MAX_TOKENS = 128


def export_text_model(output_dir: Path) -> None:
    text_dir = output_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(TEXT_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(TEXT_MODEL).eval()

    example = tokenizer("a red panda in a zoo", return_tensors="pt", padding="max_length", max_length=MAX_TOKENS)
    torch.onnx.export(
        model,
        (example["input_ids"], example["attention_mask"]),
        str(text_dir / "model.onnx"),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "logits": {0: "batch"},
        },
        dynamo=False,
    )

    tokenizer_path = hf_hub_download(TEXT_MODEL, "tokenizer.json")
    (text_dir / "tokenizer.json").write_bytes(Path(tokenizer_path).read_bytes())
    labels = {str(index): label for index, label in model.config.id2label.items()}
    (text_dir / "labels.json").write_text(json.dumps(labels, indent=2))


def fetch_nudenet_model(output_dir: Path) -> None:
    nudenet_dir = output_dir / "nudenet"
    nudenet_dir.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(NUDENET_WHEEL_INDEX) as response:
        release = json.load(response)
    wheel = next(f for f in release["urls"] if f["packagetype"] == "bdist_wheel")
    with urllib.request.urlopen(wheel["url"]) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    (nudenet_dir / "320n.onnx").write_bytes(archive.read(NUDENET_MODEL_SUFFIX))


if __name__ == "__main__":
    destination = Path(sys.argv[1])
    export_text_model(destination)
    fetch_nudenet_model(destination)
