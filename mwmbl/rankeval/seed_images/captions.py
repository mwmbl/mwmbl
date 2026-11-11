"""The image questions again, now that production shows the judge each file's Commons caption (#628).

    uv run python -m mwmbl.rankeval.seed_images.captions fetch      # captions of the files the runs below ask about
    uv run python -m mwmbl.rankeval.seed_images.captions benchmark  # the safety benchmark, separate mode, same batches
    uv run python -m mwmbl.rankeval.seed_images.captions wording    # the same, the NSFW question naming the caption
    uv run python -m mwmbl.rankeval.seed_images.captions real       # real candidates of safety.py's 200 queries
    uv run python -m mwmbl.rankeval.seed_images.captions probes     # captions that address the judge
    uv run python -m mwmbl.rankeval.seed_images.captions gate       # need + relevance on the labelled queries
    uv run python -m mwmbl.rankeval.seed_images.captions report
    uv run python -m mwmbl.rankeval.seed_images.gate_analysis --arm captions

The benchmark and the candidates were fetched before captions were, so `fetch` asks Commons for each file's
caption by title, as production's search does (English, else Commons' fallback language). A caption is today's,
so it may be newer than the description it is shown with. Each run asks exactly what injection.py and
run_hardened.py asked, plus the captions: a request with no caption in it is the same request, answered from the
cache, so only captions can move its scores. The probes are injection.py's, with the payload as the caption.
The need question doesn't read the images, so the gate keeps the hardened run's need; the `gate` run records
need only to see whether captions in the same request move it.
"""

import dataclasses
import json
import random
import sys

import django
import numpy as np
from sklearn.metrics import roc_auc_score

django.setup()

from mwmbl.imagesearch.commons import fetch_json, new_client, plain_text  # noqa: E402
from mwmbl.imagesearch.judge import nsfw_request, scores  # noqa: E402
from mwmbl.imagesearch.prompt import MIN_RELEVANCE, NEED_THRESHOLD, NSFW_THRESHOLD  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.rankeval.seed_images import run_resumably  # noqa: E402
from mwmbl.rankeval.seed_images.collect import load_candidates, with_backoff  # noqa: E402
from mwmbl.rankeval.seed_images.gate_analysis import CAPTIONS_PATH, HARDENED_PATH  # noqa: E402
from mwmbl.rankeval.seed_images.gate_data import GATE_DIR, jev_safe_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.injection import (  # noqa: E402
    CROSS_PAYLOADS,
    HARDENED_BENCHMARK_PATH,
    REAL_HARDENED_PATH,
    SELF_PAYLOADS,
    probe_batches,
    shift_line,
)
from mwmbl.rankeval.seed_images.queries import load_queries, read_jsonl  # noqa: E402
from mwmbl.rankeval.seed_images.run_hardened import score_image  # noqa: E402
from mwmbl.rankeval.seed_images.safety import (  # noqa: E402
    CACHE_DIR,
    GROUPS,
    SAFETY_DIR,
    SEED,
    benchmark_batches,
    benchmark_groups,
    benchmark_images,
    flagged_rates,
    flips,
    timed,
)

CAPTIONS_FILE = SAFETY_DIR / "captions.json"
BENCHMARK_CAPTIONS_PATH = SAFETY_DIR / "benchmark_captions.jsonl"
WORDING_PATH = SAFETY_DIR / "benchmark_captions_wording.jsonl"
REAL_CAPTIONS_PATH = SAFETY_DIR / "real_captions.jsonl"
INJECTION_CAPTIONS_PATH = SAFETY_DIR / "injection_captions.jsonl"
# Wikimedia's limit on titles per query for clients without the apihighlimits right.
TITLES_PER_REQUEST = 50
QUESTION_NAMES = "file name, description and categories"
QUESTION_NAMES_WITH_CAPTION = "file name, caption, description and categories"


def caption_params(titles: list[str]) -> dict:
    return {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "prop": "entityterms",
        "wbetterms": "label",
        "wbetlanguage": "en",
        "titles": "|".join(f"File:{title}" for title in titles),
    }


def parse_captions(body: dict) -> dict[str, str]:
    """Each title asked about, as sent, mapped to its caption, or "" if it has none or is gone."""
    query = body["query"]
    sent_as = {entry["to"]: entry["from"] for entry in query.get("normalized", [])}
    captions = {}
    for page in query["pages"]:
        labels = page.get("entityterms", {}).get("label", [])
        title = sent_as.get(page["title"], page["title"]).removeprefix("File:")
        captions[title] = plain_text(labels[0]) if labels else ""
    return captions


def load_captions() -> dict[str, str]:
    return json.loads(CAPTIONS_FILE.read_text())


def captioned(image: SourceImage, captions: dict[str, str]) -> SourceImage:
    return dataclasses.replace(image, caption=captions[image.title])


def captioned_benchmark_batches() -> list[list[tuple[dict, SourceImage]]]:
    captions = load_captions()
    batches = benchmark_batches(random.Random(SEED))
    return [[(row, captioned(image, captions)) for row, image in batch] for batch in batches]


def labelled_queries() -> list[str]:
    return sorted({row["query"] for row in read_jsonl(GATE_DIR / "labels_relevance.jsonl")})


def fetch() -> None:
    """Captions for every file the runs below send: the benchmark's, and the real and labelled queries' candidates."""
    real_queries = [row["query"] for row in read_jsonl(REAL_HARDENED_PATH)]
    safe = jev_safe_candidates()
    candidates = [image for query in real_queries for image in load_candidates(query)]
    candidates += [image for query in labelled_queries() for image in safe[query]]
    benchmark = [image for batch in benchmark_batches(random.Random(SEED)) for _, image in batch]
    captions = load_captions() if CAPTIONS_FILE.exists() else {}
    titles = sorted({image.title for image in benchmark + candidates} - set(captions))
    print(f"{len(captions)} captions fetched, {len(titles)} titles to go")
    # One request at a time, as Wikimedia asks of API clients that can wait.
    with new_client() as client:
        for start in range(0, len(titles), TITLES_PER_REQUEST):
            batch = titles[start : start + TITLES_PER_REQUEST]
            body, _ = with_backoff(lambda: fetch_json(client, caption_params(batch)))
            captions |= parse_captions(body)
            CAPTIONS_FILE.write_text(json.dumps(captions))
    print(f"{sum(map(bool, captions.values()))} of {len(captions)} files have a caption -> {CAPTIONS_FILE}")


def name_caption(body: dict) -> dict:
    """The request with each NSFW question listing the caption among the text it judges by."""
    for question in body["questions"].values():
        instructions = question["instructions"]
        instructions["question"] = instructions["question"].replace(QUESTION_NAMES, QUESTION_NAMES_WITH_CAPTION)
    return body


def run_benchmark(path, reword: bool) -> None:
    batches = captioned_benchmark_batches()

    def score_batch(index: str) -> dict:
        batch = batches[int(index)]
        request = nsfw_request([image for _, image in batch])
        call = timed(name_caption(request) if reword else request)
        batch_scores = scores(call["response"], "n", len(batch))
        record = {"batch": index, "rows": [row for row, _ in batch], "scores": batch_scores}
        return record | {"captioned": [bool(image.caption) for _, image in batch]} | call["response"]["usage"]

    run_resumably(path, "batch", [str(index) for index in range(len(batches))], score_batch)


def run_real() -> None:
    captions = load_captions()

    def score_query(query: str) -> dict:
        images = [captioned(image, captions) for image in load_candidates(query)]
        call = timed(nsfw_request(images))
        captioned_scores = scores(call["response"], "n", len(images))
        flags = [bool(image.caption) for image in images]
        return {"query": query, "scores": captioned_scores, "captioned": flags, "latency_s": call["latency_s"]}

    run_resumably(REAL_CAPTIONS_PATH, "query", [row["query"] for row in read_jsonl(REAL_HARDENED_PATH)], score_query)


def with_caption(image: SourceImage, payload: str) -> SourceImage:
    return dataclasses.replace(image, caption=payload)


def run_probes() -> None:
    """injection.py's probes, the payload replacing the caption rather than heading the description; every other
    image keeps its real caption, so a cross control is the captioned benchmark's batch."""
    captions = load_captions()
    # probe_batches() heads the descriptions with the payload, so each image is taken as the benchmark has it.
    unedited = {image.title: captioned(image, captions) for _, image in benchmark_images()}
    probes = {}
    for probe, (rows, injected_images) in probe_batches().items():
        images = [unedited[image.title] for image in injected_images]
        if probe.startswith("self-"):
            payload = SELF_PAYLOADS[probe.split("-")[1]]
            probes[probe] = (rows, [with_caption(image, payload) for image in images])
        elif probe.endswith("-injected"):
            injector, *targets = images
            probes[probe] = (rows, [with_caption(injector, CROSS_PAYLOADS[probe.split("-")[1]]), *targets])
        else:
            probes[probe] = (rows, images)

    def score_probe(probe: str) -> dict:
        rows, images = probes[probe]
        targets = scores(timed(nsfw_request(images))["response"], "n", len(images))[len(images) - len(rows) :]
        return {"probe": probe, "titles": [row["title"] for row in rows], "scores": targets}

    run_resumably(INJECTION_CAPTIONS_PATH, "probe", list(probes), score_probe)


def run_gate() -> None:
    """run_hardened's one request, with captions, on the queries with relevance labels."""
    captions = load_captions()
    queries = load_queries()
    safe = jev_safe_candidates()

    def score_query(query: str) -> dict:
        return score_image(query, queries[query], [captioned(image, captions) for image in safe[query]])

    run_resumably(CAPTIONS_PATH, "query", labelled_queries(), score_query)


def flagged_cells(groups: list[list[str]], values: list[float]) -> str:
    rates = flagged_rates(groups, values, NSFW_THRESHOLD)
    return " | ".join(f"{flagged / size:.1%} ({flagged}/{size})" for flagged, size in rates.values())


def by_batch(path) -> list[dict]:
    return sorted(read_jsonl(path), key=lambda batch: int(batch["batch"]))


def benchmark_report() -> None:
    before = by_batch(HARDENED_BENCHMARK_PATH)
    groups = [benchmark_groups(row) for batch in before for row in batch["rows"]]
    hardened = [score for batch in before for score in batch["scores"]]
    print(f"| Run (at {NSFW_THRESHOLD}) | " + " | ".join(GROUPS) + " |\n|" + "---|" * (len(GROUPS) + 1))
    print("| hardened | " + flagged_cells(groups, hardened) + " |")
    for name, path in (("captions", BENCHMARK_CAPTIONS_PATH), ("captions, reworded", WORDING_PATH)):
        batches = by_batch(path)
        values = [score for batch in batches for score in batch["scores"]]
        print(f"| {name} | " + flagged_cells(groups, values) + " |")
    for name, path in (("captions", BENCHMARK_CAPTIONS_PATH), ("captions, reworded", WORDING_PATH)):
        batches = by_batch(path)
        values = np.array([score for batch in batches for score in batch["scores"]])
        has_caption = np.array([flag for batch in batches for flag in batch["captioned"]])
        # An uncaptioned image in a request that changed was asked again, so it measures run-to-run noise.
        changed_batch = np.array([any(batch["captioned"]) for batch in batches for _ in batch["captioned"]])
        print(f"\n{name}: {has_caption.sum()} of {len(values)} images have a caption, in {sum(changed_batch)} asked")
        for subset, mask in (("captioned", has_caption), ("re-asked, uncaptioned", changed_batch & ~has_caption)):
            moved = np.array(hardened)[mask]
            print(f"  {subset}: mean shift {np.mean(values[mask] - moved):+.3f}, flips at {NSFW_THRESHOLD} ", end="")
            print(f"{flips(list(moved), list(values[mask]), NSFW_THRESHOLD)} of {mask.sum()}")
        safe = [value for value, names in zip(values, groups) if "safe" in names]
        for group in ("art", "art_evading", "photo"):
            positives = [value for value, names in zip(values, groups) if group in names]
            auc = roc_auc_score([1] * len(positives) + [0] * len(safe), positives + safe)
            print(f"  AUC {group} vs safe: {auc:.3f}")
        print(f"  mean input tokens per request: {np.mean([batch['input_tokens'] for batch in batches]):.0f}")


def real_report() -> None:
    hardened = {row["query"]: row["scores"] for row in read_jsonl(REAL_HARDENED_PATH)}
    rows = read_jsonl(REAL_CAPTIONS_PATH)
    before = np.array([score for row in rows for score in hardened[row["query"]]])
    after = np.array([score for row in rows for score in row["scores"]])
    has_caption = np.array([flag for row in rows for flag in row["captioned"]])
    passed = f"{np.mean(before < NSFW_THRESHOLD):.1%} -> {np.mean(after < NSFW_THRESHOLD):.1%}"
    print(f"\nReal candidates ({len(rows)} queries, {len(after)} images, {has_caption.sum()} captioned), ", end="")
    print(f"passed hardened -> captions: {passed}")
    captioned_flips = flips(list(before[has_caption]), list(after[has_caption]), NSFW_THRESHOLD)
    print(f"Flips at {NSFW_THRESHOLD}: {flips(list(before), list(after), NSFW_THRESHOLD)}, {captioned_flips} captioned")
    newly_blocked = np.sum((before < NSFW_THRESHOLD) & (after >= NSFW_THRESHOLD))
    newly_passed = np.sum((before >= NSFW_THRESHOLD) & (after < NSFW_THRESHOLD))
    print(f"Newly blocked {newly_blocked}, newly passed {newly_passed}")


def probes_report() -> None:
    baseline = {
        row["title"]: score
        for batch in read_jsonl(BENCHMARK_CAPTIONS_PATH)
        for row, score in zip(batch["rows"], batch["scores"])
    }
    probes = read_jsonl(INJECTION_CAPTIONS_PATH)
    print(
        "\n| Caption probe | Images | Mean shift | Flagged | Newly passed | Newly blocked |\n|---|---|---|---|---|---|"
    )
    for name in SELF_PAYLOADS:
        rows = [row for row in probes if row["probe"].startswith(f"self-{name}-")]
        before = np.array([baseline[title] for row in rows for title in row["titles"]])
        print(shift_line(f"self, {name}", before, np.array([score for row in rows for score in row["scores"]])))
    by_probe = {row["probe"]: row["scores"] for row in probes}
    for name in CROSS_PAYLOADS:
        controls = [probe for probe in by_probe if probe.startswith(f"cross-{name}-") and probe.endswith("-control")]
        before = np.array([score for probe in controls for score in by_probe[probe]])
        after = [score for probe in controls for score in by_probe[probe.replace("-control", "-injected")]]
        print(shift_line(f"cross, {name}", before, np.array(after)))


def gate_report() -> None:
    hardened = {row["query"]: row for row in read_jsonl(HARDENED_PATH)}
    rows = read_jsonl(CAPTIONS_PATH)
    before = np.array([hardened[row["query"]]["need"] for row in rows])
    after = np.array([row["need"] for row in rows])
    crossed = np.sum((before >= NEED_THRESHOLD) != (after >= NEED_THRESHOLD))
    print(f"\nNeed on {len(rows)} labelled queries: mean shift {np.mean(after - before):+.3f}, ", end="")
    print(f"{crossed} cross {NEED_THRESHOLD}, r {np.corrcoef(before, after)[0, 1]:.3f}")

    captions = load_captions()
    safe = jev_safe_candidates()
    has_caption = np.array([bool(captions[image.title]) for row in rows for image in safe[row["query"]]])
    relevance_before = np.array([score for row in rows for score in hardened[row["query"]]["scores"]])
    relevance_after = np.array([score for row in rows for score in row["scores"]])
    for subset, mask in (("captioned", has_caption), ("uncaptioned", ~has_caption)):
        moved = relevance_before[mask], relevance_after[mask]
        crossed = flips(list(moved[0]), list(moved[1]), MIN_RELEVANCE)
        print(f"Relevance, {mask.sum()} {subset}: mean shift {np.mean(moved[1] - moved[0]):+.3f}, ", end="")
        print(f"{crossed} cross {MIN_RELEVANCE}")


def report() -> None:
    benchmark_report()
    real_report()
    probes_report()
    gate_report()


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    commands = {
        "fetch": fetch,
        "benchmark": lambda: run_benchmark(BENCHMARK_CAPTIONS_PATH, reword=False),
        "wording": lambda: run_benchmark(WORDING_PATH, reword=True),
        "real": run_real,
        "probes": run_probes,
        "gate": run_gate,
        "report": report,
    }
    commands[sys.argv[1]]()
