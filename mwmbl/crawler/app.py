import gzip
import hashlib
import json
from datetime import datetime, timezone, date
from queue import Queue, Empty
from typing import Union
from uuid import uuid4

import boto3
import justext
import requests
from fastapi import HTTPException, APIRouter
from justext.core import html_to_dom, ParagraphMaker, classify_paragraphs, revise_paragraph_classification, \
    LENGTH_LOW_DEFAULT, STOPWORDS_LOW_DEFAULT, MAX_LINK_DENSITY_DEFAULT, NO_HEADINGS_DEFAULT, LENGTH_HIGH_DEFAULT, \
    STOPWORDS_HIGH_DEFAULT, MAX_HEADING_DISTANCE_DEFAULT, DEFAULT_ENCODING, DEFAULT_ENC_ERRORS, preprocessor
from redis import Redis

from mwmbl.crawler.batch import Batch, NewBatchRequest, HashedBatch
from mwmbl.crawler.stats import MwmblStats, StatsManager
from mwmbl.crawler.urls import URLDatabase, FoundURL, URLStatus
from mwmbl.database import Database
from mwmbl.format import format_result
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.indexdb import IndexDatabase, BatchInfo, BatchStatus
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
    DATE_REGEX, NUM_EXTRACT_CHARS)
from mwmbl.tinysearchengine.indexer import Document


redis = Redis(host='localhost', port=6379, decode_responses=True)


def get_bucket(name):
    s3 = boto3.resource('s3', endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=APPLICATION_KEY)
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


last_batch = None


def justext_with_dom(html_text, stoplist, length_low=LENGTH_LOW_DEFAULT,
        length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
        stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
        max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT, no_headings=NO_HEADINGS_DEFAULT,
        encoding=None, default_encoding=DEFAULT_ENCODING,
        enc_errors=DEFAULT_ENC_ERRORS):
    """
    Converts an HTML page into a list of classified paragraphs. Each paragraph
    is represented as instance of class ˙˙justext.paragraph.Paragraph˙˙.
    """
    dom = html_to_dom(html_text, default_encoding, encoding, enc_errors)

    titles = dom.xpath("//title")
    title = titles[0].text if len(titles) > 0 else None

    dom = preprocessor(dom)

    paragraphs = ParagraphMaker.make_paragraphs(dom)

    classify_paragraphs(paragraphs, stoplist, length_low, length_high,
        stopwords_low, stopwords_high, max_link_density, no_headings)
    revise_paragraph_classification(paragraphs, max_heading_distance)

    return paragraphs, title


def get_router(batch_cache: BatchCache, queued_batches: Queue):
    router = APIRouter(prefix="/crawler", tags=["crawler"])

    @router.on_event("startup")
    async def on_startup():
        with Database() as db:
            url_db = URLDatabase(db.connection)
            return url_db.create_tables()

    @router.get('/fetch')
    def fetch_url(url: str, query: str):
        response = requests.get(url)
        paragraphs, title = justext_with_dom(response.content, justext.get_stoplist("English"))
        good_paragraphs = [p for p in paragraphs if p.class_type == 'good']

        extract = ' '.join([p.text for p in good_paragraphs])
        if len(extract) > NUM_EXTRACT_CHARS:
            extract = extract[:NUM_EXTRACT_CHARS - 1] + '…'

        result = Document(title=title, url=url, extract=extract, score=0.0)
        return format_result(result, query)

    @router.post('/batches/')
    def post_batch(batch: Batch):
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

    @router.post('/batches/new')
    def request_new_batch(batch_request: NewBatchRequest) -> list[str]:
        user_id_hash = _get_user_id_hash(batch_request)
        try:
            urls = queued_batches.get(block=False)
        except Empty:
            return []

        found_urls = [FoundURL(url, user_id_hash, 0.0, URLStatus.ASSIGNED, datetime.utcnow()) for url in urls]
        with Database() as db:
            url_db = URLDatabase(db.connection)
            url_db.update_found_urls(found_urls)

        return urls

    @router.get('/batches/{date_str}/users/{public_user_id}')
    def get_batches_for_date_and_user(date_str, public_user_id):
        check_date_str(date_str)
        check_public_user_id(public_user_id)
        prefix = f'1/{VERSION}/{date_str}/1/{public_user_id}/'
        return get_batch_ids_for_prefix(prefix)

    @router.get('/batches/{date_str}/users/{public_user_id}/batch/{batch_id}')
    def get_batch_from_id(date_str, public_user_id, batch_id):
        url = get_batch_url(batch_id, date_str, public_user_id)
        data = json.loads(gzip.decompress(requests.get(url).content))
        return {
            'url': url,
            'batch': data,
        }

    @router.get('/latest-batch', response_model=list[HashedBatch])
    def get_latest_batch():
        return [] if last_batch is None else [last_batch]

    @router.get('/batches/{date_str}/users')
    def get_user_id_hashes_for_date(date_str: str):
        check_date_str(date_str)
        prefix = f'1/{VERSION}/{date_str}/1/'
        return get_subfolders(prefix)

    @router.get('/stats')
    def get_stats() -> MwmblStats:
        stats = StatsManager(redis)
        stats = stats.get_stats()
        print("Stats", stats)
        return stats

    @router.get('/')
    def status():
        return {
            'status': 'ok'
        }

    return router


def _get_user_id_hash(batch: Union[Batch, NewBatchRequest]):
    return hashlib.sha3_256(batch.user_id.encode('utf8')).hexdigest()


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HTTPException(400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}")


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
        raise HTTPException(400, f"Incorrect date format, should be YYYY-MM-DD")


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
    return result
