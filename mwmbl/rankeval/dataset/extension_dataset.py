"""
Construct a dataset from queries against search engines from
volunteers using the Firefox extension.
"""

import gzip
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH, RANKINGS_DATASET_TRAIN_PATH


LOCAL_DATASET_GLOB = Path("scripts/downloads/").glob("**/*.json.gz")
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


def create_dataset():
    dataset = []
    for path in LOCAL_DATASET_GLOB:
        print("Path", path)
        date_match = DATE_PATTERN.search(str(path))
        date_str = date_match.group(1)
        print("Date", date_str)
        with gzip.open(path) as f:
            data = json.load(f)
            search_results = data["searchResults"]
            for item in search_results:
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


if __name__ == "__main__":
    df = create_dataset()
    save_dataset(df)
