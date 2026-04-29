import gzip
import hashlib
import json
from logging import getLogger
import os
from datetime import datetime, timezone, date
from queue import Empty
from typing import Union
from uuid import uuid4

import boto3
import requests
from django.conf import settings
from ninja import NinjaAPI, Router, Schema
from ninja.errors import HttpError
from redis import Redis

from mwmbl.crawler.batch import Batch, NewBatchRequest, HashedBatch, Results, PostResultsResponse, Error, DatasetRequest, HashedDataset
from mwmbl.crawler.stats import MwmblStats, StatsManager
from mwmbl.database import Database
from mwmbl.exceptions import InvalidRequest
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index_batches import index_documents
from mwmbl.indexer.indexdb import IndexDatabase, BatchInfo, BatchStatus
from mwmbl.models import ApiKey
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.settings import (
    ENDPOINT_URL,
    KEY_ID,
    APPLICATION_KEY,
    BUCKET_NAME,
    MAX_BATCH_SIZE,
    USER_ID_LENGTH,
    VERSION,
    PUBLIC_URL_PREFIX,
    PUBLIC_USER_ID_LENGTH,
    FILE_NAME_SUFFIX,
    DATE_REGEX)
from mwmbl.tinysearchengine.indexer import Document

stats_manager = StatsManager(Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True))

logger = getLogger(__name__)

# Module-level router used by the unified v1 API
router = Router(tags=["Crawler"])


def get_bucket(name):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    logger.info(f"Uploading {len(data)} bytes to {name}")
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


last_batch = None


def upload_object(model_object: Schema, now: datetime, user_id_hash: str, object_type: str):
    seconds = (now - datetime(now.year, now.month, now.day, tzinfo=timezone.utc)).seconds

    # How to pad a string with zeros: https://stackoverflow.com/a/39402910
    # Maximum seconds in a day is 60*60*24 = 86400, so 5 digits is enough
    padded_seconds = str(seconds).zfill(5)

    # See discussion here: https://stackoverflow.com/a/13484764
    uid = str(uuid4())[:8]

    filename = f'1/{VERSION}/{now.date()}/{object_type}/{user_id_hash}/{padded_seconds}__{uid}.json.gz'
    data = gzip.compress(model_object.json().encode('utf8'))
    upload(data, filename)
    return filename


def _register_routes(r: Router | NinjaAPI, batch_cache: BatchCache, queued_batches: RedisURLQueue):
    """Register all crawler routes on the given router or API instance."""

    @r.post(
        '/batches/',
        summary="Submit a crawl batch",
        description=(
            "Deprecated - the new crawler uses the /results/ endpoint.\n\n"
            "Submit a batch of crawled pages to the Mwmbl index. "
            "Each batch must contain URLs that were previously assigned to this crawler via "
            "`POST /batches/new`. Batches are stored in object storage and queued for indexing. "
            f"Maximum {MAX_BATCH_SIZE} items per batch. "
            "The `user_id` must be exactly 64 characters."
        ),
    )
    def post_batch(request, batch: Batch):
        if len(batch.items) > MAX_BATCH_SIZE:
            return r.create_response(request, f"Batch size too large (maximum {MAX_BATCH_SIZE}), got {len(batch.items)}", status=400)

        if len(batch.user_id) != USER_ID_LENGTH:
            return r.create_response(request, f"Incorrect user ID length, should be {USER_ID_LENGTH}", status=400)

        if len(batch.items) == 0:
            return {
                'status': 'ok',
            }

        user_id_hash = _get_user_id_hash(batch)

        urls = [item.url for item in batch.items]
        invalid_urls = queued_batches.check_user_crawled_urls(user_id_hash, urls)
        if invalid_urls:
            return r.create_response(request, f"The following URLs were not assigned to the user for crawling:"
                                               f" {invalid_urls}. To suggest a domain to crawl, please visit "
                                               f"https://mwmbl.org/app/domain-submissions/new", status=400)

        # Using an approach from https://stackoverflow.com/a/30476450
        now = datetime.now(timezone.utc)
        epoch_time = (now - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        hashed_batch = HashedBatch(user_id_hash=user_id_hash, timestamp=epoch_time, items=batch.items)

        stats_manager.record_batch(hashed_batch)

        filename = upload_object(hashed_batch, now, user_id_hash, "batch")

        global last_batch
        last_batch = hashed_batch

        batch_url = f'{PUBLIC_URL_PREFIX}{filename}'
        batch_cache.store(hashed_batch, batch_url)

        # Record the batch as being local so that we don't retrieve it again when the server restarts
        infos = [BatchInfo(batch_url, user_id_hash, BatchStatus.LOCAL)]

        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.record_batches(infos)

        return {
            'status': 'ok',
            'public_user_id': user_id_hash,
            'url': batch_url,
        }

    @r.post(
        '/batches/new',
        summary="Request URLs to crawl",
        description=(
            "Deprecated - crawlers should now determine their own batches.\n\n"
            "Request a new batch of URLs assigned to this crawler for crawling. "
            "Returns a list of URLs that this crawler should fetch and submit back via "
            "`POST /batches/`. Returns an empty list if no URLs are currently queued. "
            "The `user_id` must be exactly 64 characters."
        ),
    )
    def request_new_batch(request, batch_request: NewBatchRequest) -> list[str]:
        user_id_hash = _get_user_id_hash(batch_request)
        try:
            urls = queued_batches.get_batch(user_id_hash)
        except Empty:
            return []
        return urls

    @r.get(
        '/batches/{date_str}/users/{public_user_id}',
        summary="List batch IDs for a user on a date",
        description=(
            "Retrieve the list of batch IDs submitted by a specific user on a given date. "
            "`date_str` must be in `YYYY-MM-DD` format. "
            "`public_user_id` is the SHA3-256 hash of the crawler's user ID (64 hex characters)."
        ),
    )
    def get_batches_for_date_and_user(request, date_str, public_user_id):
        check_date_str(date_str)
        check_public_user_id(public_user_id)
        prefix = f'1/{VERSION}/{date_str}/1/{public_user_id}/'
        return get_batch_ids_for_prefix(prefix)

    @r.get(
        '/batches/{date_str}/users/{public_user_id}/batch/{batch_id}',
        summary="Get a specific batch",
        description=(
            "Retrieve the full content of a specific crawl batch from object storage. "
            "`date_str` must be in `YYYY-MM-DD` format. "
            "`batch_id` is the filename stem (without extension) as returned by the list endpoint."
        ),
    )
    def get_batch_from_id(request, date_str, public_user_id, batch_id):
        url = get_batch_url(batch_id, date_str, public_user_id)
        data = json.loads(gzip.decompress(requests.get(url).content))
        return {
            'url': url,
            'batch': data,
        }

    @r.get(
        '/latest-batch',
        summary="Get the latest batch",
        description=(
            "Return the most recently submitted crawl batch held in memory. "
            "Returns an empty list if no batch has been submitted since the server started."
        ),
    )
    def get_latest_batch(request) -> list[HashedBatch]:
        return [] if last_batch is None else [last_batch]

    @r.get(
        '/batches/{date_str}/users',
        summary="List crawlers active on a date",
        description=(
            "Return the list of public user ID hashes (SHA3-256) for all crawlers that submitted "
            "batches on the given date. `date_str` must be in `YYYY-MM-DD` format."
        ),
    )
    def get_user_id_hashes_for_date(request, date_str: str):
        check_date_str(date_str)
        prefix = f'1/{VERSION}/{date_str}/1/'
        return get_subfolders(prefix)

    @r.get(
        '/stats',
        summary="Crawler statistics",
        description=(
            "Return aggregate statistics about the Mwmbl crawler network, including the number "
            "of URLs crawled, pages indexed, and active crawlers."
        ),
    )
    def get_stats(request) -> MwmblStats:
        # TODO check that the types are right here, it's not validating!
        return stats_manager.get_stats()

    @r.get(
        '/',
        summary="Health check",
        description="Returns `{\"status\": \"ok\"}` if the crawler API is running.",
    )
    def status(request):
        return {
            'status': 'ok'
        }

    @r.post(
        '/results',
        response={200: PostResultsResponse, 401: Error},
        summary="Submit indexed results",
        description=(
            "Submit a set of pre-indexed search results directly into the Mwmbl index. "
            "Requires a valid crawl-scoped API key passed in the `X-API-Key` request header "
            "(preferred) or in the request body `api_key` field (deprecated). "
            "Results are indexed immediately and also stored in object storage. "
            "This endpoint is intended for trusted crawlers."
        ),
    )
    def post_results(request, results: Results):
        # Prefer X-API-Key header; fall back to deprecated body field
        raw_key = request.headers.get("X-API-Key") or results.api_key
        if not raw_key:
            return 401, {"message": "API key required. Pass it in the X-API-Key header."}

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        api_key = ApiKey.objects.filter(
            key=key_hash,
            scopes__contains=[ApiKey.Scope.CRAWL],
        ).select_related("user").first()
        if api_key is None:
            return 401, {"message": "Invalid API key or insufficient scope (crawl scope required)."}

        documents = [Document(url=result.url, title=result.title, extract=result.extract) for result in results.results]
        index_path = f"{settings.DATA_PATH}/{settings.INDEX_NAME}"
        index_documents(documents, index_path)

        now = datetime.now(timezone.utc)
        filename = upload_object(results, now, api_key.user.username, "results")

        # Update stats for the user
        stats_manager.record_results(results, api_key.user.username)

        return {
            'status': 'ok',
            'url': f'{PUBLIC_URL_PREFIX}{filename}',
        }

    @r.post(
        '/dataset',
        summary="Submit Firefox extension dataset",
        description=(
            "Submit a dataset of search interactions collected by the Mwmbl Firefox extension. "
            "The dataset includes autocomplete interactions and search result impressions. "
            "The raw `user_id` is hashed before storage — it is never persisted in plain text. "
            "The `user_id` must be exactly 64 characters."
        ),
    )
    def post_dataset(request, dataset: DatasetRequest):
        if len(dataset.user_id) != USER_ID_LENGTH:
            return r.create_response(request, f"Incorrect user ID length, should be {USER_ID_LENGTH}", status=400)

        user_id_hash = _get_user_id_hash(dataset)

        # Create a hashed dataset that doesn't contain the raw user_id
        hashed_dataset = HashedDataset(
            user_id_hash=user_id_hash,
            date=dataset.date,
            timestamp=dataset.timestamp,
            extensionVersion=dataset.extensionVersion,
            queryDataset=dataset.queryDataset,
            searchResults=dataset.searchResults
        )

        now = datetime.now(timezone.utc)
        filename = upload_object(hashed_dataset, now, user_id_hash, "dataset")

        # Record dataset statistics
        stats_manager.record_dataset(hashed_dataset)

        return {
            'status': 'ok',
            'public_user_id': user_id_hash,
            'url': f'{PUBLIC_URL_PREFIX}{filename}',
        }


def init_router(batch_cache: BatchCache, queued_batches: RedisURLQueue):
    """Initialise the module-level router (called from urls.py for the unified v1 API)."""
    _register_routes(router, batch_cache, queued_batches)


def create_router(batch_cache: BatchCache, queued_batches: RedisURLQueue, version: str) -> NinjaAPI:
    """Create a standalone NinjaAPI for a specific version (used for legacy routes)."""
    api = NinjaAPI(urls_namespace=f"crawler-{version}")
    _register_routes(api, batch_cache, queued_batches)
    return api


def _get_user_id_hash(batch: Union[Batch, NewBatchRequest, DatasetRequest]):
    return hashlib.sha3_256(batch.user_id.encode('utf8')).hexdigest()


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HttpError(400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}")


def get_batch_url(batch_id, date_str, public_user_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    url = f'{PUBLIC_URL_PREFIX}1/{VERSION}/{date_str}/1/{public_user_id}/{batch_id}{FILE_NAME_SUFFIX}'
    return url


def get_batch_id_from_file_name(file_name: str):
    assert file_name.endswith(FILE_NAME_SUFFIX)
    return file_name[:-len(FILE_NAME_SUFFIX)]


def get_batch_ids_for_prefix(prefix):
    filenames = get_batches_for_prefix(prefix)
    filename_endings = sorted(filename.rsplit('/', 1)[1] for filename in filenames)
    results = {'batch_ids': [get_batch_id_from_file_name(name) for name in filename_endings]}
    return results


def get_batches_for_prefix(prefix):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    bucket = s3.Bucket(BUCKET_NAME)
    items = bucket.objects.filter(Prefix=prefix)
    filenames = [item.key for item in items]
    return filenames


def check_date_str(date_str):
    if not DATE_REGEX.match(date_str):
        raise HttpError(400, f"Incorrect date format, should be YYYY-MM-DD")


def get_subfolders(prefix):
    client = boto3.client('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                          aws_secret_access_key=APPLICATION_KEY)
    items = client.list_objects(Bucket=BUCKET_NAME,
                                Prefix=prefix,
                                Delimiter='/')
    item_keys = [item['Prefix'][len(prefix):].strip('/') for item in items['CommonPrefixes']]
    return item_keys


def get_batches_for_date(date_str):
    check_date_str(date_str)
    prefix = f'1/{VERSION}/{date_str}/1/'
    cache_filename = prefix + 'batches.json.gz'
    cache_url = PUBLIC_URL_PREFIX + cache_filename
    try:
        cached_batches = json.loads(gzip.decompress(requests.get(cache_url).content))
        print(f"Got cached batches for {date_str}")
        return cached_batches
    except gzip.BadGzipFile:
        pass

    batches = get_batches_for_prefix(prefix)
    result = {'batch_urls': [f'{PUBLIC_URL_PREFIX}{batch}' for batch in sorted(batches)]}
    if date_str != str(date.today()):
        # Don't cache data from today since it may change
        data = gzip.compress(json.dumps(result).encode('utf8'))
        upload(data, cache_filename)
        print(f"Cached batches for {date_str} in {PUBLIC_URL_PREFIX}{cache_filename}")
    print(f"Returning {len(result['batch_urls'])} batches for {date_str}")
    return result
