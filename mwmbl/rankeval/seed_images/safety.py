"""Phase 1 of mwmbl/mwmbl#610: is Jev's text-only NSFW check safe enough to be the only gate on Commons images?

    uv run python -m mwmbl.rankeval.seed_images.safety benchmark  # safety benchmark, separate vs combined requests
    uv run python -m mwmbl.rankeval.seed_images.safety real       # combined requests on 200 train queries
    uv run python -m mwmbl.rankeval.seed_images.safety probes     # borderline queries: collect, local layers, Jev
    uv run python -m mwmbl.rankeval.seed_images.safety report     # the tables, and texts.jsonl of disagreements

`benchmark` reads the benchmark dataset (from the image_search_benchmarks branch) and the Commons text the issue
comment's run fetched for it, both copied into devdata/seed_images/safety/. Its images are shuffled across tiers,
20 to a request; each batch is asked about once alone ("separate") and once inside a production-shaped request
for a random train query and its web pool ("combined"). Only text is sent: no thumbnail of the benchmark is fetched.
"""

import gzip
import json
import random
import sys
import time
from datetime import datetime, timezone

import django
import numpy as np
from django.conf import settings
from sklearn.metrics import roc_auc_score

django.setup()

from mwmbl.imagesearch.commons import new_client  # noqa: E402
from mwmbl.imagesearch.judge import nsfw_request, scores  # noqa: E402
from mwmbl.imagesearch.pipeline import file_id  # noqa: E402
from mwmbl.imagesearch.pixel_classifier import PixelSafetyClassifier  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.imagesearch.zero_shot_classifier import ZeroShotSafetyClassifier  # noqa: E402
from mwmbl.rankeval.evaluation.jev_order import cached_response  # noqa: E402
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR, run_resumably  # noqa: E402
from mwmbl.rankeval.seed_images.collect import candidates_path, collect_query, load_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.jev_images import combined_request  # noqa: E402
from mwmbl.rankeval.seed_images.local_verdicts import VERDICTS_PATH, judge  # noqa: E402
from mwmbl.rankeval.seed_images.queries import LLM_RELABEL_DIR, load_queries, read_jsonl, web_documents  # noqa: E402
from mwmbl.rankeval.seed_images.run_jev import NSFW_PATH, score_nsfw  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.jev_rank import NUM_LTR_CANDIDATES, jev_request  # noqa: E402

SAFETY_DIR = SEED_IMAGES_DIR / "safety"
CACHE_DIR = SAFETY_DIR / "jev_cache"
BENCHMARK_DATASET = SAFETY_DIR / "benchmark_dataset.jsonl.gz"
BENCHMARK_METADATA = SAFETY_DIR / "benchmark_metadata.json"
BENCHMARK_PATH = SAFETY_DIR / "benchmark.jsonl"
REAL_PATH = SAFETY_DIR / "real_combined.jsonl"
PROBE_VERDICTS_PATH = SAFETY_DIR / "probe_verdicts.jsonl"
PROBE_NSFW_PATH = SAFETY_DIR / "probe_nsfw.jsonl"
TEXTS_PATH = SAFETY_DIR / "texts.jsonl"
THRESHOLDS = (0.5, 1.0, 1.5, 2.0)
BATCH_SIZE = 20
NUM_REAL_QUERIES = 200
SEED = 0
LOCAL_LAYERS = ("rules", "nudenet", "zero_shot")
GROUPS = ("art", "art_evading", "photo", "photo_evading", "safe", "medical", "cleavage", "borderline")

PROBE_QUERIES = [
    *["venus", "statue of david", "birth of venus", "rubens", "venus de milo", "titian venus of urbino"],
    *["michelangelo sistine chapel", "greek statue", "roman sculpture", "the three graces", "odalisque"],
    *["adam and eve painting", "pre-raphaelite", "klimt", "egon schiele", "life drawing", "artist's model"],
    *["beach", "swimming", "sauna", "breastfeeding", "bikini", "swimsuit", "ballet", "bodybuilding"],
    *["naturism", "tattoo", "turkish bath", "skinny dipping", "pin-up", "burlesque", "playboy", "pole dance"],
    *["wrestling", "sumo", "surfing", "anatomy", "human body", "mammogram", "mastectomy", "childbirth"],
    *["breast cancer", "circumcision", "puberty", "dermatology", "nude", "naked woman", "topless beach"],
    *["erotic art", "kama sutra", "red panda", "eiffel tower", "bicycle", "chocolate cake", "volcano"],
]


def benchmark_groups(row: dict) -> list[str]:
    """The benchmark tiers an image counts towards, as the safety benchmark and the #610 comment define them."""
    evading = not row["rule_flagged"]
    groups = []
    if row["label"] == "N":
        groups += ["art", "art_evading"] if evading else ["art"]
    if row["tier"] == "photo_nude":
        groups += ["photo", "photo_evading"] if evading else ["photo"]
    if row["tier"] == "safe" or (row["tier"] == "hard" and row["label"] == "0"):
        groups.append("safe")
    if row["tier"] in ("medical", "cleavage", "borderline"):
        groups.append(row["tier"])
    return groups


def flagged_rates(groups: list[list[str]], values: list[float], threshold: float) -> dict[str, tuple[int, int]]:
    """For each group, (flagged at threshold, size)."""
    return {
        group: (
            sum(value >= threshold for value, names in zip(values, groups) if group in names),
            sum(group in names for names in groups),
        )
        for group in GROUPS
    }


def flips(first: list[float], second: list[float], threshold: float) -> int:
    return sum((a >= threshold) != (b >= threshold) for a, b in zip(first, second))


def local_flags(verdict: dict) -> list[str]:
    """The layers that would keep an image out, as in the pipeline: any verdict but SAFE on a fetched thumbnail."""
    checked = LOCAL_LAYERS if verdict["thumbnail_fetched"] else ("rules",)
    return [layer for layer in checked if verdict[layer] != "safe"]


def percentile_line(latencies: list[float]) -> str:
    p50, p95 = np.percentile(latencies, [50, 95])
    over = np.mean(np.array(latencies) > settings.JEV_TIMEOUT_SECONDS)
    return f"p50 {p50:.2f} s, p95 {p95:.2f} s, {over:.0%} over {settings.JEV_TIMEOUT_SECONDS} s"


def timed(body: dict) -> dict:
    start = time.monotonic()
    response = cached_response(body, CACHE_DIR)
    latency = time.monotonic() - start
    return {"response": response, "latency_s": latency, "questions": len(body["questions"])}


def web_pool(query: str, staan: list[dict], pools: dict[str, list[dict]]) -> list[Document]:
    """Production's pool: Staan's results, then the standard (LTR) results' top 30, each URL once."""
    standard = [candidate for candidate in pools[query] if "standard" in candidate["pools"]][:NUM_LTR_CANDIDATES]
    by_url = {result["url"]: result for result in staan + standard}
    return [Document(result["title"], result["url"], result["extract"]) for result in by_url.values()]


def combined_scores(query: str, staan: list[dict], pools: dict, images: list[SourceImage]) -> dict:
    staan_documents = web_documents({"web": staan})
    call = timed(combined_request(query, web_pool(query, staan, pools), staan_documents, images))
    nsfw_scores = scores(call["response"], "n", len(images))
    return {"scores": nsfw_scores, "latency_s": call["latency_s"], "questions": call["questions"]} | call["response"][
        "usage"
    ]


def train_context() -> tuple[dict[str, dict], dict[str, list[dict]]]:
    queries = {query: entry for query, entry in load_queries().items() if entry["set"] == "train"}
    pools = {row["query"]: row["candidates"] for row in read_jsonl(LLM_RELABEL_DIR / "pass2_pool.jsonl")}
    return queries, pools


def benchmark_images() -> list[tuple[dict, SourceImage]]:
    """Every benchmark image still on Commons when the #610 comment's run fetched its text."""
    metadata = json.loads(BENCHMARK_METADATA.read_text())
    rows = [json.loads(line) for line in gzip.open(BENCHMARK_DATASET, "rt")]
    present = [row for row in rows if row["title"] in metadata]
    return [(row, benchmark_image(row, metadata[row["title"]])) for row in present]


def benchmark_image(row: dict, metadata: dict) -> SourceImage:
    # Only the text fields reach Jev; the rest is filler, so no thumbnail of the benchmark is ever fetched.
    text = {"title": row["title"], "description": metadata["description"], "categories": tuple(metadata["categories"])}
    filler = {"page_url": "", "thumbnail_url": "", "thumbnail_width": 0, "thumbnail_height": 0, "rank": 0}
    licence = {"license_name": "", "license_url": "", "attribution": "", "sha1": row["sha1"]}
    return SourceImage(**text, **filler, **licence, uploaded_at=datetime(2000, 1, 1, tzinfo=timezone.utc))


def benchmark_batches(rng: random.Random) -> list[list[tuple[dict, SourceImage]]]:
    images = benchmark_images()
    rng.shuffle(images)
    return [images[start : start + BATCH_SIZE] for start in range(0, len(images), BATCH_SIZE)]


def run_benchmark() -> None:
    queries, pools = train_context()
    rng = random.Random(SEED)
    batches = benchmark_batches(rng)
    batch_queries = rng.choices(sorted(queries), k=len(batches))

    def score_batch(index: str) -> dict:
        batch, query = batches[int(index)], batch_queries[int(index)]
        batch_images = [image for _, image in batch]
        separate = timed(nsfw_request(batch_images))
        combined = combined_scores(query, queries[query]["web"], pools, batch_images)
        return {
            "batch": index,
            "query": query,
            "rows": [row for row, _ in batch],
            "separate": scores(separate["response"], "n", len(batch)),
            "separate_latency_s": separate["latency_s"],
            "combined": combined,
        }

    run_resumably(BENCHMARK_PATH, "batch", [str(index) for index in range(len(batches))], score_batch)


def run_real() -> None:
    queries, pools = train_context()
    separate = {row["query"]: row for row in read_jsonl(NSFW_PATH) if row["query"] in queries}
    sample = random.Random(SEED).sample(sorted(separate), NUM_REAL_QUERIES)

    def score_query(query: str) -> dict:
        staan = queries[query]["web"]
        web_only = timed(jev_request(query, web_pool(query, staan, pools)))
        combined = combined_scores(query, staan, pools, load_candidates(query))
        web_only_record = {"latency_s": web_only["latency_s"], "questions": web_only["questions"]}
        web_only_record |= web_only["response"]["usage"]
        return {"query": query, "separate": separate[query], "combined": combined, "web_only": web_only_record}

    run_resumably(REAL_PATH, "query", sample, score_query)


def run_probes() -> None:
    with new_client() as client:
        for query in PROBE_QUERIES:
            if not candidates_path(query).exists():
                collect_query(client, query)
        candidates = {file_id(image): image for query in PROBE_QUERIES for image in load_candidates(query)}
        nudenet = PixelSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
        zero_shot = ZeroShotSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
        run_resumably(
            PROBE_VERDICTS_PATH, "file_id", list(candidates), lambda f: judge(client, candidates[f], nudenet, zero_shot)
        )
    run_resumably(PROBE_NSFW_PATH, "query", [query for query in PROBE_QUERIES if load_candidates(query)], score_nsfw)


def benchmark_report() -> None:
    batches = read_jsonl(BENCHMARK_PATH)
    groups = [benchmark_groups(row) for batch in batches for row in batch["rows"]]
    modes = {
        "separate": [score for batch in batches for score in batch["separate"]],
        "combined": [score for batch in batches for score in batch["combined"]["scores"]],
    }
    print(f"## Benchmark: {len(groups)} images in {len(batches)} requests\n")
    print("| Mode | Threshold | " + " | ".join(GROUPS) + " |\n|" + "---|" * (len(GROUPS) + 2))
    for mode, values in modes.items():
        for threshold in THRESHOLDS:
            rates = flagged_rates(groups, values, threshold)
            cells = [f"{flagged / size:.1%} ({flagged}/{size})" for flagged, size in rates.values()]
            print(f"| {mode} | {threshold} | " + " | ".join(cells) + " |")
    for mode, values in modes.items():
        safe = [value for value, names in zip(values, groups) if "safe" in names]
        for group in ("art", "art_evading", "photo"):
            positives = [value for value, names in zip(values, groups) if group in names]
            labels = [1] * len(positives) + [0] * len(safe)
            print(f"AUC {mode} {group} vs safe: {roc_auc_score(labels, positives + safe):.3f}")
    print(f"Pearson r separate vs combined: {np.corrcoef(modes['separate'], modes['combined'])[0, 1]:.3f}")
    print("Flips: " + ", ".join(f"{t}: {flips(modes['separate'], modes['combined'], t)}" for t in THRESHOLDS))
    print("Separate latency: " + percentile_line([batch["separate_latency_s"] for batch in batches]))
    print("Combined latency: " + percentile_line([batch["combined"]["latency_s"] for batch in batches]))


def real_report() -> None:
    rows = read_jsonl(REAL_PATH)
    separate = [score for row in rows for score in row["separate"]["scores"]]
    combined = [score for row in rows for score in row["combined"]["scores"]]
    print(f"\n## Real train queries: {len(rows)} queries, {len(separate)} images\n")
    print(f"Pearson r: {np.corrcoef(separate, combined)[0, 1]:.3f}")
    print(f"Mean |difference|: {np.mean(np.abs(np.array(separate) - np.array(combined))):.3f}")
    print("Flips: " + ", ".join(f"{t}: {flips(separate, combined, t)}" for t in THRESHOLDS))
    for name, key in (("Separate NSFW", "separate"), ("Web only", "web_only"), ("Combined", "combined")):
        latencies = [row[key]["latency_s"] for row in rows]
        print(f"{name} latency: {percentile_line(latencies)}")
    for name in ("web_only", "combined"):
        means = {
            key: np.mean([row[name][key] for row in rows]) for key in ("questions", "input_tokens", "output_tokens")
        }
        print(f"{name} mean per request: " + ", ".join(f"{key} {value:.0f}" for key, value in means.items()))


def disagreement_report(name: str, nsfw_rows: list[dict], verdicts: dict[str, dict], texts) -> None:
    occurrences = [
        (row["query"], image, score, verdicts[file_id(image)])
        for row in nsfw_rows
        for image, score in zip(load_candidates(row["query"]), row["scores"])
    ]
    unchecked = sum(not verdict["thumbnail_fetched"] for _, _, _, verdict in occurrences)
    print(f"\n## {name}: {len(nsfw_rows)} queries, {len(occurrences)} images, {unchecked} thumbnails unchecked\n")
    print("| Jev passes (<) | Passed | Any local flag | Rules | NudeNet | SigLIP2 | Queries affected |\n|" + "---|" * 7)
    for threshold in THRESHOLDS:
        passed = [occurrence for occurrence in occurrences if occurrence[2] < threshold]
        flagged = [occurrence for occurrence in passed if local_flags(occurrence[3])]
        by_layer = [sum(layer in local_flags(verdict) for _, _, _, verdict in flagged) for layer in LOCAL_LAYERS]
        affected = len({query for query, _, _, _ in flagged})
        print(
            f"| {threshold} | {len(passed)} | {len(flagged)} | " + " | ".join(map(str, by_layer)) + f" | {affected} |"
        )
    jev_only = [o for o in occurrences if o[2] >= 1.5 and not local_flags(o[3]) and o[3]["thumbnail_fetched"]]
    print(f"Jev >= 1.5 but every local layer passes: {len(jev_only)}")
    for query, image, score, verdict in occurrences:
        if (score < 1.5 and local_flags(verdict)) or (score >= 1.5 and not local_flags(verdict)):
            record = {"set": name, "query": query, "jev": score, "flags": local_flags(verdict)}
            text = {"title": image.title, "description": image.description[:300], "categories": image.categories}
            texts.write(json.dumps(record | text | {"siglip2": verdict.get("zero_shot_probability")}) + "\n")


def report() -> None:
    benchmark_report()
    real_report()
    verdicts = {row["file_id"]: row for row in read_jsonl(VERDICTS_PATH) + read_jsonl(PROBE_VERDICTS_PATH)}
    with TEXTS_PATH.open("w") as texts:
        disagreement_report("Real queries", read_jsonl(NSFW_PATH), verdicts, texts)
        disagreement_report("Probe queries", read_jsonl(PROBE_NSFW_PATH), verdicts, texts)


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    commands = {"benchmark": run_benchmark, "real": run_real, "probes": run_probes, "report": report}
    commands[sys.argv[1]]()
