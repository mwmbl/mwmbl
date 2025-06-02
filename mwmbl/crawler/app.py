import gzip
import hashlib
import json
import os
from datetime import datetime, timezone, date
from queue import Empty
from typing import Union
from uuid import uuid4

import boto3
import requests
from django.conf import settings
from ninja import NinjaAPI, Schema, HTTPException
from redis import Redis

from mwmbl.crawler.batch import (
    Batch,
    NewBatchRequest,
    HashedBatch,
    Results,
    PostResultsResponse,
    Error,
)
from mwmbl.crawler.env_vars import REDIS_URL
from mwmbl.crawl import CRAWLER_VERSION
from mwmbl.crawler.stats import MwmblStats, StatsManager
from mwmbl.database import Database
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
    DATE_REGEX,
)
from mwmbl.tinysearchengine.indexer import Document

stats_manager = StatsManager(Redis.from_url(REDIS_URL, decode_responses=True))


def get_bucket(name):
    s3 = boto3.resource(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=APPLICATION_KEY,
    )
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


last_batch = None


def create_router(
    batch_cache: BatchCache, queued_batches: RedisURLQueue, version: str
) -> NinjaAPI:
    router = NinjaAPI(urls_namespace=f"crawler-{version}")

    @router.post("/batches/")
    def post_batch(request, batch: Batch):
        """
        Store a batch of crawled pages.
        """

        if len(batch.items) > MAX_BATCH_SIZE:
            return router.create_response(
                request,
                f"Batch size too large (maximum {MAX_BATCH_SIZE}), got {len(batch.items)}",
                status=400,
            )

        if len(batch.user_id) != USER_ID_LENGTH:
            return router.create_response(
                request,
                f"Incorrect user ID length, should be {USER_ID_LENGTH}",
                status=400,
            )

        if len(batch.items) == 0:
            return {
                "status": "ok",
            }

        user_id_hash = _get_user_id_hash(batch)

        urls = [item.url for item in batch.items]
        invalid_urls = queued_batches.check_user_crawled_urls(user_id_hash, urls)
        if invalid_urls:
            return router.create_response(
                request,
                f"The following URLs were not assigned to the user for crawling:"
                f" {invalid_urls}. To suggest a domain to crawl, please visit "
                f"https://mwmbl.org/app/domain-submissions/new",
                status=400,
            )

        # Using an approach from https://stackoverflow.com/a/30476450
        now = datetime.now(timezone.utc)
        epoch_time = (now - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        hashed_batch = HashedBatch(
            user_id_hash=user_id_hash,
            timestamp=epoch_time,
            items=batch.items,
            crawler_version=CRAWLER_VERSION,
        )

        stats_manager.record_batch(hashed_batch)

        filename = upload_object(hashed_batch, now, user_id_hash, "batch")

        global last_batch
        last_batch = hashed_batch

        batch_url = f"{PUBLIC_URL_PREFIX}{filename}"
        batch_cache.store(hashed_batch, batch_url)

        # Record the batch as being local so that we don't retrieve it again when the server restarts
        infos = [BatchInfo(batch_url, user_id_hash, BatchStatus.LOCAL)]

        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.record_batches(infos)

        return {
            "status": "ok",
            "public_user_id": user_id_hash,
            "url": batch_url,
        }

    def upload_object(
        model_object: Schema, now: datetime, user_id_hash: str, object_type: str
    ):
        seconds = (
            now - datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        ).seconds

        # How to pad a string with zeros: https://stackoverflow.com/a/39402910
        # Maximum seconds in a day is 60*60*24 = 86400, so 5 digits is enough
        padded_seconds = str(seconds).zfill(5)

        # See discussion here: https://stackoverflow.com/a/13484764
        uid = str(uuid4())[:8]

        filename = f"1/{VERSION}/{now.date()}/{object_type}/{user_id_hash}/{padded_seconds}__{uid}.json.gz"
        data = gzip.compress(model_object.json().encode("utf8"))
        upload(data, filename)
        return filename

    @router.post("/batches/new")
    def request_new_batch(request, batch_request: NewBatchRequest) -> list[str]:
        user_id_hash = _get_user_id_hash(batch_request)
        try:
            urls = queued_batches.get_batch(user_id_hash)
        except Empty:
            return []
        return urls

    @router.get("/batches/{date_str}/users/{public_user_id}")
    def get_batches_for_date_and_user(request, date_str, public_user_id):
        check_date_str(date_str)
        check_public_user_id(public_user_id)
        prefix = f"1/{VERSION}/{date_str}/1/{public_user_id}/"
        return get_batch_ids_for_prefix(prefix)

    @router.get("/batches/{date_str}/users/{public_user_id}/batch/{batch_id}")
    def get_batch_from_id(request, date_str, public_user_id, batch_id):
        url = get_batch_url(batch_id, date_str, public_user_id)
        data = json.loads(gzip.decompress(requests.get(url).content))
        return {
            "url": url,
            "batch": data,
        }

    @router.get("/latest-batch")
    def get_latest_batch(request) -> list[HashedBatch]:
        return [] if last_batch is None else [last_batch]

    @router.get("/batches/{date_str}/users")
    def get_user_id_hashes_for_date(request, date_str: str):
        check_date_str(date_str)
        prefix = f"1/{VERSION}/{date_str}/1/"
        return get_subfolders(prefix)

    @router.get("/stats")
    def get_stats(request) -> MwmblStats:
        # TODO check that the types are right here, it's not validating!
        return stats_manager.get_stats()

    @router.get("/")
    def status(request):
        return {"status": "ok"}

    @router.post("/results", response={200: PostResultsResponse, 401: Error})
    def post_results(request, results: Results):
        # Check the API key
        api_key = ApiKey.objects.filter(key=results.api_key).first()
        if api_key is None:
            return 401, {"message": "Invalid API key"}

        documents = [
            Document(url=result.url, title=result.title, extract=result.extract)
            for result in results.results
        ]
        index_path = f"{settings.DATA_PATH}/{settings.INDEX_NAME}"
        index_documents(documents, index_path)

        now = datetime.now(timezone.utc)
        filename = upload_object(results, now, api_key.user.username, "results")

        # Update stats for the user
        stats_manager.record_results(results, api_key.user.username)

        return {
            "status": "ok",
            "url": f"{PUBLIC_URL_PREFIX}{filename}",
        }

    return router


def _get_user_id_hash(batch: Union[Batch, NewBatchRequest]):
    return hashlib.sha3_256(batch.user_id.encode("utf8")).hexdigest()


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HTTPException(
            400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}"
        )


def get_batch_url(batch_id, date_str, public_user_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    url = f"{PUBLIC_URL_PREFIX}1/{VERSION}/{date_str}/1/{public_user_id}/{batch_id}{FILE_NAME_SUFFIX}"
    return url


def get_batch_id_from_file_name(file_name: str):
    assert file_name.endswith(FILE_NAME_SUFFIX)
    return file_name[: -len(FILE_NAME_SUFFIX)]


def get_batch_ids_for_prefix(prefix):
    filenames = get_batches_for_prefix(prefix)
    filename_endings = sorted(filename.rsplit("/", 1)[1] for filename in filenames)
    results = {
        "batch_ids": [get_batch_id_from_file_name(name) for name in filename_endings]
    }
    return results


def get_batches_for_prefix(prefix):
    s3 = boto3.resource(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=APPLICATION_KEY,
    )
    bucket = s3.Bucket(BUCKET_NAME)
    items = bucket.objects.filter(Prefix=prefix)
    filenames = [item.key for item in items]
    return filenames


def check_date_str(date_str):
    if not DATE_REGEX.match(date_str):
        raise HTTPException(400, f"Incorrect date format, should be YYYY-MM-DD")


def get_subfolders(prefix):
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=APPLICATION_KEY,
    )
    items = client.list_objects(Bucket=BUCKET_NAME, Prefix=prefix, Delimiter="/")
    item_keys = [
        item["Prefix"][len(prefix) :].strip("/") for item in items["CommonPrefixes"]
    ]
    return item_keys


def get_batches_for_date(date_str):
    check_date_str(date_str)
    prefix = f"1/{VERSION}/{date_str}/1/"
    cache_filename = prefix + "batches.json.gz"
    cache_url = PUBLIC_URL_PREFIX + cache_filename
    try:
        cached_batches = json.loads(gzip.decompress(requests.get(cache_url).content))
        print(f"Got cached batches for {date_str}")
        return cached_batches
    except gzip.BadGzipFile:
        pass

    batches = get_batches_for_prefix(prefix)
    result = {
        "batch_urls": [f"{PUBLIC_URL_PREFIX}{batch}" for batch in sorted(batches)]
    }
    if date_str != str(date.today()):
        # Don't cache data from today since it may change
        data = gzip.compress(json.dumps(result).encode("utf8"))
        upload(data, cache_filename)
        print(f"Cached batches for {date_str} in {PUBLIC_URL_PREFIX}{cache_filename}")
    print(f"Returning {len(result['batch_urls'])} batches for {date_str}")
    return result
