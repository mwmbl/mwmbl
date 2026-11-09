"""Tests for the two model wrappers, with a tiny tokenizer and fake ONNX sessions.

CI does not have the real models, so these run the wrappers' own code (tokenizing, chunking,
softmax, preprocessing, thresholds, decoding failures) around a session that answers by rule.
What the real models say is checked by the skipped-when-absent tests in test_image_search.py.
"""

import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from mwmbl.imagesearch import pipeline, pixel_classifier, text_classifier, zero_shot_classifier
from mwmbl.imagesearch.pixel_classifier import BOX_VALUES, CLASS_NAMES, INPUT_SIZE, PixelSafetyClassifier
from mwmbl.imagesearch.text_classifier import (
    BATCH_SIZE,
    MAX_CHUNKS,
    MAX_TOKENS,
    WORDS_PER_CHUNK,
    TextSafetyClassifier,
)
from mwmbl.imagesearch.types import Verdict
from mwmbl.imagesearch.zero_shot_classifier import INPUT_SIZE as ZERO_SHOT_INPUT_SIZE
from mwmbl.imagesearch.zero_shot_classifier import ZeroShotSafetyClassifier

SAFE_WORD, BAD_WORD, MAYBE_WORD, FILLER = 1, 2, 3, 4
VOCAB = {"[UNK]": 0, "safe": SAFE_WORD, "bad": BAD_WORD, "maybe": MAYBE_WORD, "x": FILLER, "[CLS]": 5, "[SEP]": 6}
# Softmax of these logits gives an NSFW probability of about 0, 0.1 and 1, which the thresholds
# in settings read as safe, uncertain and NSFW.
SAFE_LOGITS = [5.0, -5.0]
UNCERTAIN_LOGITS = [0.0, float(np.log(1 / 9))]
NSFW_LOGITS = [-5.0, 5.0]


class FakeTextSession:
    """Reads the words as a rule: 'bad' makes a text NSFW, 'maybe' uncertain, anything else safe."""

    options_seen: list = []
    batch_shapes: list = []

    def __init__(self, path, options, providers):
        self.options_seen.append(options)

    def run(self, output_names, inputs):
        def logits_for(ids):
            if BAD_WORD in ids:
                return NSFW_LOGITS
            return UNCERTAIN_LOGITS if MAYBE_WORD in ids else SAFE_LOGITS

        assert output_names == ["logits"]
        assert inputs["input_ids"].shape == inputs["attention_mask"].shape
        self.batch_shapes.append(inputs["input_ids"].shape)
        return [np.array([logits_for(row) for row in inputs["input_ids"]])]


@pytest.fixture
def text_model_dir(tmp_path, monkeypatch):
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    tokenizer = Tokenizer(models.WordLevel(VOCAB, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    # Like the real model's tokenizer; without a post-processor, truncation does not report overflow.
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 5), ("[SEP]", 6)]
    )
    tokenizer.save(str(text_dir / "tokenizer.json"))
    (text_dir / "labels.json").write_text(json.dumps({"0": "safe", "1": "nsfw"}))
    (text_dir / "model.onnx").write_bytes(b"text model")
    FakeTextSession.options_seen = []
    FakeTextSession.batch_shapes = []
    monkeypatch.setattr(text_classifier.onnxruntime, "InferenceSession", FakeTextSession)
    return tmp_path


@pytest.fixture
def text_model(text_model_dir):
    return TextSafetyClassifier(text_model_dir)


def test_text_model_reads_each_text_as_safe_uncertain_or_nsfw(text_model):
    texts = ["safe safe", "safe bad", "maybe safe"]
    assert text_model.classify(texts) == [Verdict.SAFE, Verdict.NSFW, Verdict.UNCERTAIN]


def test_text_model_finds_the_nsfw_class_from_the_label_map(text_model_dir):
    (text_model_dir / "text" / "labels.json").write_text(json.dumps({"0": "NSFW", "1": "safe"}))
    assert TextSafetyClassifier(text_model_dir).nsfw_index == 0


def test_text_model_with_no_texts_has_no_verdicts(text_model):
    assert text_model.classify([]) == []


def test_a_long_text_is_judged_by_its_worst_chunk(text_model):
    clean = "safe " * (WORDS_PER_CHUNK * 3)
    assert text_model.classify([clean, clean + "bad"]) == [Verdict.SAFE, Verdict.NSFW]


def test_text_too_long_to_check_or_empty_is_uncertain(text_model):
    too_long = "safe " * (WORDS_PER_CHUNK * MAX_CHUNKS + 1)
    assert text_model.classify([too_long, ""]) == [Verdict.UNCERTAIN, Verdict.UNCERTAIN]


def test_a_chunk_longer_than_the_models_window_is_uncertain(text_model):
    # One whitespace-separated word that tokenizes into more tokens than the model reads.
    crammed = ",".join(["x"] * (MAX_TOKENS * 2))
    assert text_model.classify([crammed, "safe"]) == [Verdict.UNCERTAIN, Verdict.SAFE]


def test_each_text_keeps_its_own_verdict_when_batched_with_others(text_model):
    texts = ["bad", "", "safe", "maybe " * (WORDS_PER_CHUNK + 1)]
    assert text_model.classify(texts) == [Verdict.NSFW, Verdict.UNCERTAIN, Verdict.SAFE, Verdict.UNCERTAIN]


def test_text_model_trims_padding_and_keeps_each_verdict_in_the_order_given(text_model):
    # Lengths run from 3 tokens to 25, and the NSFW texts are spread across short and long.
    texts = [("safe " * count).strip() + (" bad" if count % 4 == 0 else "") for count in range(1, 24)]
    expected = [Verdict.NSFW if count % 4 == 0 else Verdict.SAFE for count in range(1, 24)]

    assert text_model.classify(texts) == expected

    shapes = FakeTextSession.batch_shapes
    assert sum(rows for rows, _ in shapes) == len(texts)
    assert all(rows <= BATCH_SIZE for rows, _ in shapes)
    longest_text_tokens = 23 + 2
    assert max(width for _, width in shapes) == longest_text_tokens
    # Sorted by length, so the first batch holds the shortest texts and is not padded to the longest.
    assert shapes[0][1] < longest_text_tokens


def test_text_model_session_uses_the_configured_threads(text_model_dir, settings):
    settings.IMAGE_SEARCH_TEXT_THREADS = 3
    TextSafetyClassifier(text_model_dir)
    (options,) = FakeTextSession.options_seen
    assert options.intra_op_num_threads == 3


# Pixel model


def fake_pixel_session(**scores_by_class):
    """A session class whose detector sees the given classes at the given scores in every image."""
    output = np.zeros((1, BOX_VALUES + len(CLASS_NAMES), 3))
    for name, score in scores_by_class.items():
        output[0, BOX_VALUES + CLASS_NAMES.index(name), 1] = score

    class FakePixelSession:
        inputs_seen: list = []
        options_seen: list = []

        def __init__(self, path, options, providers):
            self.options_seen.append(options)

        def get_inputs(self):
            return [SimpleNamespace(name="images")]

        def run(self, output_names, inputs):
            self.inputs_seen.append(inputs["images"])
            return [output]

    FakePixelSession.inputs_seen = []
    FakePixelSession.options_seen = []
    return FakePixelSession


def pixel_model(monkeypatch, tmp_path, **scores_by_class) -> PixelSafetyClassifier:
    (tmp_path / "nudenet").mkdir(exist_ok=True)
    model_path = tmp_path / "nudenet" / "320n.onnx"
    if not model_path.exists():
        model_path.write_bytes(b"pixel model")
    monkeypatch.setattr(pixel_classifier.onnxruntime, "InferenceSession", fake_pixel_session(**scores_by_class))
    return PixelSafetyClassifier(tmp_path)


def encoded(image: Image.Image, format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()


def test_pixel_model_reads_its_scores_as_a_verdict(monkeypatch, tmp_path):
    data = encoded(Image.new("RGB", (64, 64)))
    assert pixel_model(monkeypatch, tmp_path).classify_bytes(data) is Verdict.SAFE
    assert pixel_model(monkeypatch, tmp_path, FEMALE_BREAST_EXPOSED=0.9).classify_bytes(data) is Verdict.NSFW
    assert pixel_model(monkeypatch, tmp_path, BUTTOCKS_COVERED=0.8).classify_bytes(data) is Verdict.UNCERTAIN
    assert pixel_model(monkeypatch, tmp_path, FACE_FEMALE=1.0).classify_bytes(data) is Verdict.SAFE


def test_pixel_model_session_uses_the_configured_threads(monkeypatch, tmp_path, settings):
    settings.IMAGE_SEARCH_PIXEL_THREADS = 1
    classifier = pixel_model(monkeypatch, tmp_path)
    (options,) = type(classifier.session).options_seen
    assert options.intra_op_num_threads == 1


def test_pixel_fingerprint_changes_with_the_model_file_and_the_thresholds(monkeypatch, tmp_path, settings):
    before = pixel_model(monkeypatch, tmp_path).fingerprint
    assert pixel_model(monkeypatch, tmp_path).fingerprint == before

    settings.IMAGE_SEARCH_PIXEL_EXPOSED_THRESHOLD = 0.9
    assert pixel_model(monkeypatch, tmp_path).fingerprint != before
    settings.IMAGE_SEARCH_PIXEL_EXPOSED_THRESHOLD = 0.2

    (tmp_path / "nudenet" / "320n.onnx").write_bytes(b"a newer pixel model")
    assert pixel_model(monkeypatch, tmp_path).fingerprint != before


def test_pixel_input_is_a_padded_square_in_the_models_layout(monkeypatch, tmp_path):
    classifier = pixel_model(monkeypatch, tmp_path)
    classifier.classify_bytes(encoded(Image.new("RGB", (200, 100), (255, 255, 255))))
    (model_input,) = type(classifier.session).inputs_seen
    assert model_input.shape == (1, 3, INPUT_SIZE, INPUT_SIZE)
    assert model_input.dtype == np.float32
    # The image is padded at the bottom, not stretched: the top is white and the bottom is blank.
    assert model_input[0, :, 10, 10].tolist() == [1.0, 1.0, 1.0]
    assert model_input[0, :, INPUT_SIZE - 10, 10].tolist() == [0.0, 0.0, 0.0]


def test_pixel_model_accepts_images_that_are_not_rgb(monkeypatch, tmp_path):
    classifier = pixel_model(monkeypatch, tmp_path)
    assert classifier.classify_bytes(encoded(Image.new("L", (32, 32)))) is Verdict.SAFE
    assert classifier.classify_bytes(encoded(Image.new("RGBA", (32, 32)))) is Verdict.SAFE


def test_pixel_model_will_not_vouch_for_bytes_it_cannot_decode(monkeypatch, tmp_path):
    classifier = pixel_model(monkeypatch, tmp_path)
    assert classifier.classify_bytes(b"not an image") is Verdict.UNCERTAIN
    assert classifier.classify_bytes(b"") is Verdict.UNCERTAIN


def test_pixel_model_will_not_vouch_for_a_decompression_bomb(monkeypatch, tmp_path):
    classifier = pixel_model(monkeypatch, tmp_path)
    bomb = encoded(Image.new("1", (3500, 3500)))
    assert classifier.classify_bytes(bomb) is Verdict.UNCERTAIN


# Loading the models for the pipeline


def test_the_pipeline_loads_each_model_once_from_the_configured_directory(text_model_dir, monkeypatch, settings):
    settings.IMAGE_SEARCH_MODEL_DIR = text_model_dir
    (text_model_dir / "nudenet").mkdir()
    (text_model_dir / "nudenet" / "320n.onnx").write_bytes(b"pixel model")
    monkeypatch.setattr(pixel_classifier.onnxruntime, "InferenceSession", fake_pixel_session())
    pipeline.text_classifier.cache_clear()
    pipeline.pixel_classifier.cache_clear()
    try:
        assert isinstance(pipeline.text_classifier(), TextSafetyClassifier)
        assert pipeline.text_classifier() is pipeline.text_classifier()
        assert isinstance(pipeline.pixel_classifier(), PixelSafetyClassifier)
        assert pipeline.pixel_classifier() is pipeline.pixel_classifier()
    finally:
        pipeline.text_classifier.cache_clear()
        pipeline.pixel_classifier.cache_clear()


# Zero-shot model

# Unit prompt embeddings in two dimensions: the nude prompts point along the first axis, the safe ones along the second.
NUDE_EMBEDDINGS = [[1.0, 0.0], [0.8, 0.6]]
SAFE_EMBEDDINGS = [[0.0, 1.0], [0.6, 0.8]]
LOGIT_SCALE = 10.0


class FakeZeroShotSession:
    """Embeds a bright image along the nude axis and a dark one along the safe axis."""

    options_seen: list = []
    inputs_seen: list = []

    def __init__(self, path, options, providers):
        self.options_seen.append(options)

    def get_inputs(self):
        return [SimpleNamespace(name="pixel_values")]

    def run(self, output_names, inputs):
        assert output_names == ["features"]
        pixels = inputs["pixel_values"]
        self.inputs_seen.append(pixels)
        return [np.array([[3.0, 0.0] if pixels.mean() > 0 else [0.0, 3.0]])]


@pytest.fixture
def zero_shot_model_dir(tmp_path, monkeypatch):
    zero_shot_dir = tmp_path / "zero_shot"
    zero_shot_dir.mkdir()
    (zero_shot_dir / "model.onnx").write_bytes(b"zero-shot model")
    np.save(zero_shot_dir / "text_embeddings.npy", np.array(NUDE_EMBEDDINGS + SAFE_EMBEDDINGS, dtype=np.float32))
    (zero_shot_dir / "prompts.json").write_text(json.dumps({"nude": ["a", "b"], "safe": ["c", "d"]}))
    (zero_shot_dir / "logit.json").write_text(json.dumps({"scale": LOGIT_SCALE, "bias": 0.0}))
    FakeZeroShotSession.options_seen = []
    FakeZeroShotSession.inputs_seen = []
    monkeypatch.setattr(zero_shot_classifier.onnxruntime, "InferenceSession", FakeZeroShotSession)
    return tmp_path


@pytest.fixture
def zero_shot_model(zero_shot_model_dir):
    return ZeroShotSafetyClassifier(zero_shot_model_dir)


def test_zero_shot_probability_is_the_softmax_mass_on_the_nude_prompts(zero_shot_model):
    logits = LOGIT_SCALE * np.array([1.0, 0.8, 0.0, 0.6])
    expected = np.exp(logits)[:2].sum() / np.exp(logits).sum()
    assert zero_shot_model.nude_probability(Image.new("RGB", (64, 64), "white")) == pytest.approx(expected)
    assert zero_shot_model.nude_probability(Image.new("RGB", (64, 64), "black")) < 0.05


def test_zero_shot_does_not_depend_on_the_scale_of_the_features(zero_shot_model):
    # Only the direction of the image embedding matters: it is normalised before it meets the prompts.
    probability = zero_shot_model.nude_probability(Image.new("RGB", (64, 64), "white"))
    zero_shot_model.session.run = lambda names, inputs: [np.array([[300.0, 0.0]])]
    assert zero_shot_model.nude_probability(Image.new("RGB", (64, 64), "white")) == pytest.approx(probability)


def test_zero_shot_reads_its_probability_as_a_verdict_at_the_threshold(zero_shot_model, settings):
    white, black = encoded(Image.new("RGB", (64, 64), "white")), encoded(Image.new("RGB", (64, 64), "black"))
    assert zero_shot_model.classify_bytes(white) is Verdict.NSFW
    assert zero_shot_model.classify_bytes(black) is Verdict.SAFE

    settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD = 0.9999
    assert zero_shot_model.classify_bytes(white) is Verdict.SAFE
    settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD = 0.0001
    assert zero_shot_model.classify_bytes(black) is Verdict.NSFW


def test_zero_shot_will_not_vouch_for_bytes_it_cannot_decode(zero_shot_model):
    assert zero_shot_model.classify_bytes(b"not an image") is Verdict.UNCERTAIN
    assert zero_shot_model.classify_bytes(b"") is Verdict.UNCERTAIN
    assert zero_shot_model.classify_bytes(encoded(Image.new("1", (3500, 3500)))) is Verdict.UNCERTAIN


def test_zero_shot_input_is_a_stretched_square_normalised_to_minus_one_to_one(zero_shot_model):
    model_input = zero_shot_model.preprocess(Image.new("RGB", (200, 100), (255, 255, 255)))
    assert model_input.shape == (1, 3, ZERO_SHOT_INPUT_SIZE, ZERO_SHOT_INPUT_SIZE)
    assert model_input.dtype == np.float32
    assert model_input.min() == model_input.max() == 1.0
    # A wide image is resized to the square rather than cropped, so a stripe near one edge survives.
    striped = Image.new("RGB", (200, 100), "black")
    striped.paste("white", (190, 0, 200, 100))
    assert zero_shot_model.preprocess(striped)[0, 0, :, -1].min() > 0


def test_zero_shot_accepts_images_that_are_not_rgb(zero_shot_model):
    for mode in ("L", "RGBA", "P"):
        assert zero_shot_model.classify_bytes(encoded(Image.new(mode, (32, 32)))) in (Verdict.SAFE, Verdict.NSFW)


def test_zero_shot_session_uses_the_configured_threads(zero_shot_model_dir, settings):
    settings.IMAGE_SEARCH_ZERO_SHOT_THREADS = 3
    ZeroShotSafetyClassifier(zero_shot_model_dir)
    (options,) = FakeZeroShotSession.options_seen
    assert options.intra_op_num_threads == 3


def test_zero_shot_fingerprint_changes_with_the_model_the_prompts_and_the_threshold(zero_shot_model_dir, settings):
    zero_shot_dir = zero_shot_model_dir / "zero_shot"
    before = ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint
    assert ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint == before

    settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD = 0.9
    assert ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint != before
    settings.IMAGE_SEARCH_ZERO_SHOT_THRESHOLD = 0.38
    assert ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint == before

    for name, content in (("model.onnx", b"another model"), ("logit.json", json.dumps({"scale": 1.0, "bias": 0.0}))):
        original = (zero_shot_dir / name).read_bytes()
        (zero_shot_dir / name).write_bytes(content if isinstance(content, bytes) else content.encode())
        assert ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint != before
        (zero_shot_dir / name).write_bytes(original)

    np.save(zero_shot_dir / "text_embeddings.npy", np.array(NUDE_EMBEDDINGS + SAFE_EMBEDDINGS, dtype=np.float32) * 0.5)
    assert ZeroShotSafetyClassifier(zero_shot_model_dir).fingerprint != before


def test_zero_shot_embeddings_must_match_the_prompts(zero_shot_model_dir):
    (zero_shot_model_dir / "zero_shot" / "prompts.json").write_text(json.dumps({"nude": ["a"], "safe": ["b"]}))
    with pytest.raises(ValueError, match="one row per prompt"):
        ZeroShotSafetyClassifier(zero_shot_model_dir)


def test_the_pipeline_loads_the_zero_shot_model_once_from_the_configured_directory(zero_shot_model_dir, settings):
    settings.IMAGE_SEARCH_MODEL_DIR = zero_shot_model_dir
    pipeline.zero_shot_classifier.cache_clear()
    try:
        assert isinstance(pipeline.zero_shot_classifier(), ZeroShotSafetyClassifier)
        assert pipeline.zero_shot_classifier() is pipeline.zero_shot_classifier()
    finally:
        pipeline.zero_shot_classifier.cache_clear()
