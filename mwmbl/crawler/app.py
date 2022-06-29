import gzip
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from typing import Union
from urllib.parse import urlparse
from uuid import uuid4

import boto3
import requests
from fastapi import HTTPException, APIRouter

from mwmbl.crawler.batch import Batch, NewBatchRequest, HashedBatch
from mwmbl.crawler.urls import URLDatabase, FoundURL, URLStatus
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.indexdb import IndexDatabase
from mwmbl.tinysearchengine.indexer import Document

APPLICATION_KEY = os.environ['MWMBL_APPLICATION_KEY']
KEY_ID = os.environ['MWMBL_KEY_ID']
ENDPOINT_URL = 'https://s3.us-west-004.backblazeb2.com'
BUCKET_NAME = 'mwmbl-crawl'
MAX_BATCH_SIZE = 100
USER_ID_LENGTH = 36
PUBLIC_USER_ID_LENGTH = 64
VERSION = 'v1'
DATE_REGEX = re.compile(r'\d{4}-\d{2}-\d{2}')
PUBLIC_URL_PREFIX = f'https://f004.backblazeb2.com/file/{BUCKET_NAME}/'
FILE_NAME_SUFFIX = '.json.gz'

SCORE_FOR_ROOT_PATH = 0.1
SCORE_FOR_DIFFERENT_DOMAIN = 1.0
SCORE_FOR_SAME_DOMAIN = 0.01


router = APIRouter(prefix="/crawler", tags=["crawler"])


def get_bucket(name):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


last_batch = None


@router.on_event("startup")
async def on_startup():
    with Database() as db:
        url_db = URLDatabase(db.connection)
        return url_db.create_tables()


@router.post('/batches/')
def create_batch(batch: Batch):
    if len(batch.items) > MAX_BATCH_SIZE:
        raise HTTPException(400, f"Batch size too large (maximum {MAX_BATCH_SIZE}), got {len(batch.items)}")

    if len(batch.user_id) != USER_ID_LENGTH:
        raise HTTPException(400, f"User ID length is incorrect, should be {USER_ID_LENGTH} characters")

    if len(batch.items) == 0:
        return {
            'status': 'ok',
        }

    user_id_hash = _get_user_id_hash(batch)

    now = datetime.now(timezone.utc)
    seconds = (now - datetime(now.year, now.month, now.day, tzinfo=timezone.utc)).seconds

    # How to pad a string with zeros: https://stackoverflow.com/a/39402910
    # Maximum seconds in a day is 60*60*24 = 86400, so 5 digits is enough
    padded_seconds = str(seconds).zfill(5)

    # See discussion here: https://stackoverflow.com/a/13484764
    uid = str(uuid4())[:8]
    filename = f'1/{VERSION}/{now.date()}/1/{user_id_hash}/{padded_seconds}__{uid}.json.gz'

    # Using an approach from https://stackoverflow.com/a/30476450
    epoch_time = (now - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
    hashed_batch = HashedBatch(user_id_hash=user_id_hash, timestamp=epoch_time, items=batch.items)
    data = gzip.compress(hashed_batch.json().encode('utf8'))
    upload(data, filename)

    record_urls_in_database(batch, user_id_hash, now)
    queue_batch(hashed_batch)

    global last_batch
    last_batch = hashed_batch

    return {
        'status': 'ok',
        'public_user_id': user_id_hash,
        'url': f'{PUBLIC_URL_PREFIX}{filename}',
    }


def _get_user_id_hash(batch: Union[Batch, NewBatchRequest]):
    return hashlib.sha3_256(batch.user_id.encode('utf8')).hexdigest()


@router.post('/batches/new')
def request_new_batch(batch_request: NewBatchRequest):
    user_id_hash = _get_user_id_hash(batch_request)

    with Database() as db:
        url_db = URLDatabase(db.connection)
        return url_db.get_new_batch_for_user(user_id_hash)


@router.post('/batches/historical')
def create_historical_batch(batch: HashedBatch):
    """
    Update the database state of URL crawling for old data
    """
    user_id_hash = batch.user_id_hash
    batch_datetime = get_datetime_from_timestamp(batch.timestamp)
    record_urls_in_database(batch, user_id_hash, batch_datetime)


def get_datetime_from_timestamp(timestamp: int) -> datetime:
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    return batch_datetime


def record_urls_in_database(batch: Union[Batch, HashedBatch], user_id_hash: str, timestamp: datetime):
    with Database() as db:
        url_db = URLDatabase(db.connection)
        url_scores = defaultdict(float)
        for item in batch.items:
            if item.content is not None:
                crawled_page_domain = urlparse(item.url).netloc
                if crawled_page_domain not in DOMAINS:
                    continue

                for link in item.content.links:
                    parsed_link = urlparse(link)
                    score = SCORE_FOR_SAME_DOMAIN if parsed_link.netloc == crawled_page_domain else SCORE_FOR_DIFFERENT_DOMAIN
                    url_scores[link] += score
                    domain = f'{parsed_link.scheme}://{parsed_link.netloc}/'
                    url_scores[domain] += SCORE_FOR_ROOT_PATH

        batch_datetime = get_datetime_from_timestamp(batch.timestamp)
        found_urls = [FoundURL(url, user_id_hash, score, URLStatus.NEW, batch_datetime) for url, score in url_scores.items()]
        if len(found_urls) > 0:
            url_db.update_found_urls(found_urls)

        crawled_urls = [FoundURL(item.url, user_id_hash, 0.0, URLStatus.CRAWLED, batch_datetime)
                        for item in batch.items]
        url_db.update_found_urls(crawled_urls)

        # TODO:
        #  - delete existing crawl data for change from INT to FLOAT


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
    return result


def get_user_id_hash_from_url(url):
    return url.split('/')[9]


@router.get('/batches/{date_str}/users/{public_user_id}')
def get_batches_for_date_and_user(date_str, public_user_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    prefix = f'1/{VERSION}/{date_str}/1/{public_user_id}/'
    return get_batch_ids_for_prefix(prefix)


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HTTPException(400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}")


@router.get('/batches/{date_str}/users/{public_user_id}/batch/{batch_id}')
def get_batch_from_id(date_str, public_user_id, batch_id):
    url = get_batch_url(batch_id, date_str, public_user_id)
    data = json.loads(gzip.decompress(requests.get(url).content))
    return {
        'url': url,
        'batch': data,
    }


def get_batch_url(batch_id, date_str, public_user_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    url = f'{PUBLIC_URL_PREFIX}1/{VERSION}/{date_str}/1/{public_user_id}/{batch_id}{FILE_NAME_SUFFIX}'
    return url


@router.get('/latest-batch', response_model=list[HashedBatch])
def get_latest_batch():
    return [] if last_batch is None else [last_batch]


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


@router.get('/batches/{date_str}/users')
def get_user_id_hashes_for_date(date_str: str):
    check_date_str(date_str)
    prefix = f'1/{VERSION}/{date_str}/1/'
    return get_subfolders(prefix)


def check_date_str(date_str):
    if not DATE_REGEX.match(date_str):
        raise HTTPException(400, f"Incorrect date format, should be YYYY-MM-DD")


def get_subfolders(prefix):
    client = boto3.client('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                          aws_secret_access_key=APPLICATION_KEY)
    items = client.list_objects(Bucket=BUCKET_NAME,
                                Prefix=prefix,
                                Delimiter='/')
    item_keys = [item['Prefix'][len(prefix):].strip('/') for item in items['CommonPrefixes']]
    return item_keys


@router.get('/')
def status():
    return {
        'status': 'ok'
    }


def queue_batch(batch: HashedBatch):
    # TODO: get the score from the URLs database
    # TODO: also queue documents for batches sent through the API
    documents = [Document(item.content.title, item.url, item.content.extract, 1)
                 for item in batch.items if item.content is not None]
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.queue_documents(documents)