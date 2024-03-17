import os
from datetime import date
from logging import getLogger
from pathlib import Path

from django.conf import settings
from redis import Redis

from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.utils import batch

INDEX_URL_COUNT_KEY = "index-count-{date}"
LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30
NUM_PAGES_IN_BATCH = 1024


logger = getLogger(__name__)


def get_redis():
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)


def count_urls() -> int:
    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    redis = get_redis()

    logger.info(f"Counting URLs in index {index_path}")

    key = INDEX_URL_COUNT_KEY.format(date=date.today())
    with TinyIndex(item_factory=Document, index_path=index_path) as tiny_index:
        # Count using a Redis hyperloglog to avoid memory issues.
        for page_batch in batch(range(tiny_index.num_pages), NUM_PAGES_IN_BATCH):
            urls = set()
            for page in page_batch:
                urls |= {doc.url for doc in tiny_index.get_page(page)}
            redis.pfadd(key, *urls)
            logger.info(f"Counted {page} pages of {tiny_index.num_pages}.")

    count = redis.pfcount(key)
    redis.expire(key, LONG_EXPIRE_SECONDS)
    logger.info("Counted %d unique URLs", count)

    return count


def get_url_count(date_: date) -> int:
    redis = get_redis()
    key = INDEX_URL_COUNT_KEY.format(date_)
    return redis.pfcount(key)


if __name__ == "__main__":
    # configure logging
    import logging
    logging.basicConfig(level=logging.INFO)

    count_urls()
