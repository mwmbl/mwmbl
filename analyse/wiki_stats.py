"""
Download Wikipedia statistics, aggregate and store in a file.
"""
import gzip
import json
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from random import Random
from typing import Iterable

import requests
from joblib import Memory

WIKI_STATS_URL_FORMAT = ("https://dumps.wikimedia.org/other/pageviews/"
                         "{year}/{year}-{month:02d}/pageviews-{year}{month:02d}{day:02d}-{hour:02d}0000.gz")

OUTPUT_PATH = Path(__file__).parent.parent / "mwmbl" / "resources" / "wiki_stats.json"


random = Random(1)
memory = Memory()


def get_wiki_stats_urls(n: int = 10):
    # Choose ten random hours from the last month
    today = date.today()
    max_date = datetime(today.year, today.month, today.day) - timedelta(days=1)
    seen_hours = set()
    for _ in range(n):
        hours_ago = random.randint(0, 24 * 30)
        if hours_ago in seen_hours:
            continue
        seen_hours.add(hours_ago)

        url_date = max_date - timedelta(hours=hours_ago)
        url = WIKI_STATS_URL_FORMAT.format(year=url_date.year, month=url_date.month, day=url_date.day, hour=url_date.hour)
        yield url


@memory.cache()
def get_en_stats(url: str):
    print(f"Downloading {url}")
    response = requests.get(url)
    print("Decompressing...")
    content = gzip.decompress(response.content)
    return [line for line in content.decode().split("\n") if line.startswith("en")]


def download_wiki_stats_files(urls: Iterable[str]):
    for url in urls:
        en_stats = get_en_stats(url)
        yield from en_stats


def aggregate_stats(stats: Iterable[str], num_titles: int = 100_000):
    title_counts = defaultdict(int)

    for stat in stats:
        _, title, count, _ = stat.split(" ")
        title_counts[title] += int(count)

    top_counts = dict(sorted(title_counts.items(), key=lambda x: x[1], reverse=True)[:num_titles])

    print("Saving to", OUTPUT_PATH)
    with OUTPUT_PATH.open("w") as f:
        json.dump(top_counts, f, indent=2)


def run():
    urls = get_wiki_stats_urls(n=10)
    stats = download_wiki_stats_files(urls)
    aggregate_stats(stats, 500_000)


if __name__ == "__main__":
    run()







