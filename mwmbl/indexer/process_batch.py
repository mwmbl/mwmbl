from logging import getLogger
from typing import Callable, Collection

from mwmbl.crawler.batch import HashedBatch
from mwmbl.database import Database
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.indexdb import BatchStatus, IndexDatabase

logger = getLogger(__name__)


def run(batch_cache: BatchCache, start_status: BatchStatus, end_status: BatchStatus,
        process: Callable[[Collection[HashedBatch]], None]):

    with Database() as db:
        index_db = IndexDatabase(db.connection)

        logger.info(f"Getting batches with status {start_status}")
        batches = index_db.get_batches_by_status(start_status, 10000)
        logger.info(f"Got {len(batches)} batch urls")
        if len(batches) == 0:
            return

        batch_data = batch_cache.get_cached([batch.url for batch in batches])
        logger.info(f"Got {len(batch_data)} cached batches")

        process(batch_data.values())

        index_db.update_batch_status([batch.url for batch in batches], end_status)
