"""Evaluation harness for Wikimedia Commons images in Seed Search (mwmbl/mwmbl#610).

Run in order, from the repo root, with `.env` sourced (`set -a && source .env && set +a`) and
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev:

    uv run python -m mwmbl.rankeval.seed_images.queries ARMS    # queries.json + the en-gb baseline lists
    uv run python -m mwmbl.rankeval.seed_images.collect         # top Commons candidates per query
    uv run python -m mwmbl.rankeval.seed_images.local_verdicts  # rules, NudeNet and SigLIP2 on each candidate
    uv run python -m mwmbl.rankeval.seed_images.run_jev         # Jev's NSFW and image-need scores

Each step is resumable and caches what it fetched under devdata/seed_images/.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from django.conf import settings

SEED_IMAGES_DIR = Path(settings.BASE_DIR) / "devdata" / "seed_images"
QUERIES_PATH = SEED_IMAGES_DIR / "queries.json"
CANDIDATES_DIR = SEED_IMAGES_DIR / "candidates"
WORKERS = 4


def run_resumably(path: Path, key: str, items: list[str], score: Callable[[str], dict]) -> None:
    """Append score(item) to the JSON-lines file for each item whose `key` is not in it yet, WORKERS at a time."""
    done = {json.loads(line)[key] for line in path.open()} if path.exists() else set()
    remaining = [item for item in items if item not in done]
    print(f"{path.name}: {len(items) - len(remaining)} done, {len(remaining)} to go")
    with ThreadPoolExecutor(WORKERS) as executor, path.open("a") as out:
        for record in executor.map(score, remaining):
            out.write(json.dumps(record) + "\n")
            out.flush()
