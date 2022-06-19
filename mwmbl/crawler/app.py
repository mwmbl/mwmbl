import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Union
from uuid import uuid4

import boto3
import requests
from fastapi import HTTPException, APIRouter
from pydantic import BaseModel

from mwmbl.crawler.urls import URLDatabase
from mwmbl.database import Database

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


router = APIRouter(prefix="/crawler", tags=["crawler"])


def get_bucket(name):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


class ItemContent(BaseModel):
    title: str
    extract: str
    links: list[str]


class ItemError(BaseModel):
    name: str
    message: Optional[str]


class Item(BaseModel):
    url: str
    status: Optional[int]
    timestamp: int
    content: Optional[ItemContent]
    error: Optional[ItemError]


class Batch(BaseModel):
    user_id: str
    items: list[Item]


class NewBatchRequest(BaseModel):
    user_id: str


class HashedBatch(BaseModel):
    user_id_hash: str
    timestamp: int
    items: list[Item]


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

    _record_urls_in_database(batch, user_id_hash, now)

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
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=batch.timestamp)
    _record_urls_in_database(batch, user_id_hash, batch_datetime)


def _record_urls_in_database(batch: Union[Batch, HashedBatch], user_id_hash: str, timestamp: datetime):
    with Database() as db:
        url_db = URLDatabase(db.connection)
        found_urls = set()
        for item in batch.items:
            if item.content is not None:
                found_urls |= set(item.content.links)

        if len(found_urls) > 0:
            url_db.user_found_urls(user_id_hash, list(found_urls), timestamp)

        crawled_urls = [item.url for item in batch.items]
        url_db.user_crawled_urls(user_id_hash, crawled_urls, timestamp)


@router.get('/batches/{date_str}/users/{public_user_id}')
def get_batches_for_date_and_user(date_str, public_user_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    prefix = f'1/{VERSION}/{date_str}/1/{public_user_id}/'
    return get_batches_for_prefix(prefix)


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HTTPException(400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}")


@router.get('/batches/{date_str}/users/{public_user_id}/batch/{batch_id}')
def get_batch_from_id(date_str, public_user_id, batch_id):
    check_date_str(date_str)
    check_public_user_id(public_user_id)
    url = f'{PUBLIC_URL_PREFIX}1/{VERSION}/{date_str}/1/{public_user_id}/{batch_id}{FILE_NAME_SUFFIX}'
    data = json.loads(gzip.decompress(requests.get(url).content))
    return data


@router.get('/latest-batch', response_model=list[HashedBatch])
def get_latest_batch():
    return [] if last_batch is None else [last_batch]


def get_batch_id_from_file_name(file_name: str):
    assert file_name.endswith(FILE_NAME_SUFFIX)
    return file_name[:-len(FILE_NAME_SUFFIX)]


def get_batches_for_prefix(prefix):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    bucket = s3.Bucket(BUCKET_NAME)
    items = bucket.objects.filter(Prefix=prefix)
    file_names = sorted(item.key.rsplit('/', 1)[1] for item in items)
    results = {'batch_ids': [get_batch_id_from_file_name(name) for name in file_names]}
    return results


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
    print("Got items", items)
    item_keys = [item['Prefix'][len(prefix):].strip('/') for item in items['CommonPrefixes']]
    return item_keys


@router.get('/')
def status():
    return {
        'status': 'ok'
    }
