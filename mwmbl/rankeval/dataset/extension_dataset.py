"""
Build the rank-evaluation gold dataset from Firefox-extension search scrapes.

Volunteers running the Mwmbl Firefox extension submit the search results they
are shown by commercial search engines. The server stores each submission in
the Backblaze bucket under ``1/<VERSION>/<date>/dataset/<user-hash>/...`` as a
gzipped JSON ``DatasetRequest`` (see ``mwmbl.crawler.batch``). This module
downloads those files and turns them into the train/test gold-ranking CSVs that
``mwmbl.rankeval.evaluation.evaluate`` scores ranking models against.

Usage::

    # Download any new scrapes from Backblaze, then (re)build the CSVs:
    uv run python -m mwmbl.rankeval.dataset.extension_dataset

    # Rebuild the CSVs from already-downloaded files, without hitting Backblaze:
    uv run python -m mwmbl.rankeval.dataset.extension_dataset --no-download

Downloading requires Backblaze credentials (``MWMBL_KEY_ID`` /
``MWMBL_APPLICATION_KEY``), read from the environment or a repo-root ``.env``
file. Files already present in ``scripts/downloads/`` are skipped, so re-runs
only fetch newly-submitted scrapes.
"""

import argparse
import gzip
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH, RANKINGS_DATASET_TRAIN_PATH
from mwmbl.settings import BUCKET_NAME, ENDPOINT_URL, VERSION


REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADS_DIR = REPO_ROOT / "scripts" / "downloads"
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _bucket():
    """Connect to the Backblaze bucket, loading credentials from .env if needed.

    The keys are read from the environment here (after ``load_dotenv``) rather
    than from ``mwmbl.settings``, whose ``KEY_ID`` / ``APPLICATION_KEY`` are
    bound at import time — before this .env is loaded.
    """
    load_dotenv(REPO_ROOT / ".env")
    s3 = boto3.resource(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=os.environ["MWMBL_KEY_ID"],
        aws_secret_access_key=os.environ["MWMBL_APPLICATION_KEY"],
    )
    return s3.Bucket(BUCKET_NAME)


def download_datasets(workers: int = 16):
    """Download every extension dataset file from Backblaze into ``scripts/downloads/``.

    The local layout mirrors the bucket keys, and files already present are
    skipped so re-runs only fetch new submissions. Downloads run concurrently
    (small files, network-bound) over a thread pool.
    """
    bucket = _bucket()
    client = bucket.meta.client

    # Dataset scrapes live at 1/<VERSION>/<date>/dataset/<user-hash>/<file>.json.gz.
    # List the date subfolders first, then the dataset files under each one; the
    # batch crawl data sharing these date prefixes is far larger and irrelevant.
    base = f"1/{VERSION}/"
    paginator = client.get_paginator("list_objects_v2")
    date_prefixes = []
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=base, Delimiter="/"):
        date_prefixes += [cp["Prefix"] for cp in page.get("CommonPrefixes", [])]

    keys = []
    for date_prefix in date_prefixes:
        keys += [obj.key for obj in bucket.objects.filter(Prefix=f"{date_prefix}dataset/")]

    missing = [k for k in keys if not (DOWNLOADS_DIR / k).exists()]
    print(f"{len(keys)} dataset files on Backblaze, {len(keys) - len(missing)} already "
          f"present locally; downloading {len(missing)}...")

    def _fetch(key: str):
        dest = DOWNLOADS_DIR / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = client.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
        dest.write_bytes(body)

    downloaded = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, _ in enumerate(as_completed(pool.submit(_fetch, k) for k in missing), 1):
            downloaded = i
            if i % 500 == 0:
                print(f"Downloaded {i}/{len(missing)} new files...")

    print(f"Download complete: {downloaded} new files, "
          f"{len(keys) - len(missing)} already present.")


def create_dataset() -> pd.DataFrame:
    """Flatten every downloaded scrape into (query, url, snippet, rank) rows."""
    dataset = []
    for path in DOWNLOADS_DIR.glob("**/*.json.gz"):
        date_match = DATE_PATTERN.search(str(path))
        if date_match is None:
            continue
        date_str = date_match.group(1)
        with gzip.open(path) as f:
            data = json.load(f)
        for item in data.get("searchResults", []):
            query = item["query"]
            for i, row in enumerate(item["results"]):
                dataset.append({
                    "query": query,
                    "url": row["url"],
                    "snippet": row["extract"],
                    "rank": i + 1,
                    "date_retrieved": date_str,
                })

    return pd.DataFrame(dataset)


np_random = np.random.RandomState(1)


def save_dataset(dataset: pd.DataFrame):
    RANKINGS_DATASET_TEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    queries = dataset["query"].unique()
    train_size = int(0.8 * len(queries))
    train_queries = np_random.choice(queries, train_size)

    train_set = dataset[dataset["query"].isin(train_queries)]
    test_set = dataset[~dataset["query"].isin(train_queries)]

    print(f"Saving dataset with {len(train_set)} train rows "
          f"and {len(test_set)} test rows to {RANKINGS_DATASET_TRAIN_PATH.parent}")

    train_set.to_csv(RANKINGS_DATASET_TRAIN_PATH)
    test_set.to_csv(RANKINGS_DATASET_TEST_PATH)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--no-download", action="store_true",
        help="Skip the Backblaze download and rebuild the CSVs from "
             "files already in scripts/downloads/.")
    args = parser.parse_args()

    if not args.no_download:
        download_datasets()
    df = create_dataset()
    save_dataset(df)


if __name__ == "__main__":
    main()
