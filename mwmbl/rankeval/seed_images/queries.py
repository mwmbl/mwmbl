"""The two query sets, each query with the web top ten it is served, in one file the other steps read.

train: the 849 LLM-labelled queries, with Staan's results in Staan's order. engb: the 295 en-gb evaluation
queries, in production's order (Jev composite + Staan rank), also written as a holistic-judge baseline lists file.
"""

import json
from argparse import ArgumentParser
from pathlib import Path

from django.conf import settings

from mwmbl.rankeval.seed_images import QUERIES_PATH, SEED_IMAGES_DIR

LLM_RELABEL_DIR = Path(settings.BASE_DIR) / "devdata" / "llm_relabel"
PRODUCTION_ARM = "Jev composite + Staan rank"
ENGB_BASELINE_PATH = SEED_IMAGES_DIR / "engb_baseline_lists.json"
NUM_RESULTS = 10


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open()]


def train_queries() -> dict[str, dict]:
    intents = {row["query"]: row["intent"] for row in read_jsonl(LLM_RELABEL_DIR / "pass1_intents.jsonl")}
    staan = {row["query"]: row["results"] for row in read_jsonl(LLM_RELABEL_DIR / "pass2_staan.jsonl")}
    return {
        query: {
            "set": "train",
            "intent": intent,
            "web": [{key: result[key] for key in ("url", "title", "extract")} for result in staan[query][:NUM_RESULTS]],
        }
        for query, intent in intents.items()
    }


def engb_queries(arms_path: Path) -> dict[str, dict]:
    arms = json.loads(arms_path.read_text())
    queries = {}
    for query, entry in arms.items():
        top_urls = entry["lists"][PRODUCTION_ARM][:NUM_RESULTS]
        web = [{"url": url, "title": entry["pages"][url][0], "extract": entry["pages"][url][1]} for url in top_urls]
        queries[query] = {"set": "engb", "intent": None, "web": web}
    return queries


def load_queries() -> dict[str, dict]:
    return json.loads(QUERIES_PATH.read_text())


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("engb_arms", type=Path, help="engb_jev_composite_arms.json from the mwmbl#462 experiments")
    args = parser.parse_args()

    train = train_queries()
    engb = engb_queries(args.engb_arms)
    assert not set(train) & set(engb), "the two query sets overlap"
    SEED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    QUERIES_PATH.write_text(json.dumps(train | engb))
    baseline_lists = {query: entry["web"] for query, entry in engb.items()}
    ENGB_BASELINE_PATH.write_text(json.dumps(baseline_lists))
    print(f"{len(train)} train + {len(engb)} engb queries -> {QUERIES_PATH}; baseline -> {ENGB_BASELINE_PATH}")


if __name__ == "__main__":
    main()
