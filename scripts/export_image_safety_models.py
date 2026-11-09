"""Build the ONNX models that image search's safety pipeline runs.

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
- zero_shot/: google/siglip2-base-patch16-224 (Apache-2.0) for the zero-shot nudity layer. model.onnx is the image
  tower (fp32: int8 quantization cut recall from 94% to 75%); text_embeddings.npy, prompts.json and logit.json are
  the unit-norm embeddings of the fixed prompts below and the model's temperature, so the server needs no tokenizer.

Point IMAGE_SEARCH_MODEL_DIR at the output directory.
"""

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from transformers import AutoModel, AutoModelForSequenceClassification, AutoProcessor, AutoTokenizer

TEXT_MODEL = "eliasalbouzidi/distilbert-nsfw-text-classifier"
NUDENET_WHEEL_INDEX = "https://pypi.org/pypi/nudenet/json"
NUDENET_MODEL_SUFFIX = "nudenet/320n.onnx"
MAX_TOKENS = 128
ZERO_SHOT_MODEL = "google/siglip2-base-patch16-224"
ZERO_SHOT_INPUT_SIZE = 224
# Fixed before any results were seen and not tuned. The art-specific prompts carry the result: with only the
# photo-style ones the model separates art nudes from clean images no better than AUC 0.71.
NUDE_PROMPTS = [
    "a nude person",
    "a photo of a naked person",
    "a painting of a nude figure",
    "a statue of a nude figure",
    "an erotic image",
    "a sexually explicit image",
    "exposed breasts",
    "exposed genitals",
    "a topless woman",
    "a drawing of a naked person",
    "a person in lingerie",
    "a sexually suggestive pose",
]
SAFE_PROMPTS = [
    "a photo of a clothed person",
    "a landscape",
    "an animal",
    "a building",
    "an everyday object",
    "a painting of a clothed person",
    "a statue of a clothed person",
    "a diagram",
    "a map or text document",
    "food",
    "a vehicle",
    "a plant",
]


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


def pooled(output):
    """The embedding tensor, whether this transformers version returns it bare or inside a ModelOutput."""
    return getattr(output, "pooler_output", output)


class ImageTower(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, pixel_values):
        return pooled(self.model.get_image_features(pixel_values=pixel_values))


def export_zero_shot_model(output_dir: Path) -> None:
    zero_shot_dir = output_dir / "zero_shot"
    zero_shot_dir.mkdir(parents=True, exist_ok=True)

    processor = AutoProcessor.from_pretrained(ZERO_SHOT_MODEL)
    model = AutoModel.from_pretrained(ZERO_SHOT_MODEL).float().eval()

    example = torch.zeros(1, 3, ZERO_SHOT_INPUT_SIZE, ZERO_SHOT_INPUT_SIZE)
    torch.onnx.export(
        ImageTower(model),
        (example,),
        str(zero_shot_dir / "model.onnx"),
        input_names=["pixel_values"],
        output_names=["features"],
        dynamic_axes={"pixel_values": {0: "batch"}, "features": {0: "batch"}},
        dynamo=False,
    )

    prompts = NUDE_PROMPTS + SAFE_PROMPTS
    with torch.inference_mode():
        text_inputs = processor(text=prompts, return_tensors="pt", padding="max_length")
        embeddings = pooled(model.get_text_features(**text_inputs))
        embeddings = embeddings / embeddings.norm(dim=-1, keepdim=True)
    np.save(zero_shot_dir / "text_embeddings.npy", embeddings.numpy().astype(np.float32))
    (zero_shot_dir / "prompts.json").write_text(json.dumps({"nude": NUDE_PROMPTS, "safe": SAFE_PROMPTS}, indent=2))
    logit = {"scale": model.logit_scale.exp().item(), "bias": model.logit_bias.item()}
    (zero_shot_dir / "logit.json").write_text(json.dumps(logit, indent=2))


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
    export_zero_shot_model(destination)
