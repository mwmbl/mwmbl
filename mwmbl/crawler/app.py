import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from logging import getLogger
from uuid import uuid4

import boto3
import requests
import sentry_sdk
from django.conf import settings
from ninja import NinjaAPI, Router, Schema
from ninja.errors import HttpError
from redis import Redis

from mwmbl.crawler import worst_agreements
from mwmbl.crawler.agreement import record_agreements
from mwmbl.crawler.batch import (
    CrawlerErrorReport,
    DatasetRequest,
    Error,
    HashedDataset,
    PostResultsResponse,
    Results,
    StatusResponse,
)
from mwmbl.crawler.stats import MwmblStats, StatsManager
from mwmbl.devices import record_device
from mwmbl.indexer.index_batches import index_documents
from mwmbl.models import ApiKey
from mwmbl.settings import (
    APPLICATION_KEY,
    BUCKET_NAME,
    DATE_REGEX,
    ENDPOINT_URL,
    FILE_NAME_SUFFIX,
    KEY_ID,
    NUM_EXTRACT_CHARS,
    NUM_TITLE_CHARS,
    PUBLIC_URL_PREFIX,
    PUBLIC_USER_ID_LENGTH,
    USER_ID_LENGTH,
    VERSION,
)
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.utils import utc_today

stats_manager = StatsManager(
    Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
)

logger = getLogger(__name__)


def _truncated(text: str, limit: int) -> str:
    """The text cut to the limit the crawler cuts it to, so a client that does not cannot
    store more: a result is shown at that length, and its text is copied to every page of
    its terms and kept for comparison with other crawlers' copies."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


INVALID_CRAWL_KEY_MESSAGE = "Invalid API key or insufficient scope (crawl scope required)."

# Module-level router used by the unified v1 API
router = Router(tags=["Crawler"])


def get_bucket(name):
    s3 = boto3.resource(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    logger.info(f"Uploading {len(data)} bytes to {name}")
    bucket = get_bucket(name)
    result = bucket.put(Body=data)
    return result


def upload_object(model_object: Schema, now: datetime, user_id_hash: str, object_type: str):
    seconds = (now - datetime(now.year, now.month, now.day, tzinfo=timezone.utc)).seconds

    # How to pad a string with zeros: https://stackoverflow.com/a/39402910
    # Maximum seconds in a day is 60*60*24 = 86400, so 5 digits is enough
    padded_seconds = str(seconds).zfill(5)

    # See discussion here: https://stackoverflow.com/a/13484764
    uid = str(uuid4())[:8]

    filename = f"1/{VERSION}/{now.date()}/{object_type}/{user_id_hash}/{padded_seconds}__{uid}.json.gz"
    data = gzip.compress(model_object.json().encode("utf8"))
    upload(data, filename)
    return filename


def _register_routes(r: Router | NinjaAPI):
    """Register all crawler routes on the given router or API instance."""

    @r.post(
        "/batches/",
        summary="Submit a crawl batch (removed)",
        description=(
            "Removed - this endpoint served the old crawler and now always returns 410 Gone.\n\n"
            "Crawlers should submit crawled pages via `POST /results/` instead."
        ),
    )
    def post_batch(request):
        # HttpError rather than r.create_response: `r` may be a Router, which has no
        # create_response.
        raise HttpError(410, "This endpoint has been removed. Submit crawled pages via POST /results/ instead.")

    @r.post(
        "/batches/new",
        summary="Request URLs to crawl (removed)",
        description=(
            "Removed - this handed out URLs to be submitted back via the now-removed "
            "`POST /batches/`, so it always returns 410 Gone.\n\n"
            "Crawlers now choose their own URLs and submit them via `POST /results/`."
        ),
    )
    def request_new_batch(request):
        # Not an empty list: get_batch() pops URLs off the queue permanently, so every
        # legacy crawler still polling this was draining the crawl frontier into results
        # it could no longer submit. An error tells its operator the client is dead;
        # an empty list would have left it polling a queue it can never contribute to.
        raise HttpError(410, "This endpoint has been removed. Crawlers now choose their own URLs.")

    @r.get(
        "/batches/{date_str}/users/{public_user_id}",
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
        prefix = f"1/{VERSION}/{date_str}/1/{public_user_id}/"
        return get_batch_ids_for_prefix(prefix)

    @r.get(
        "/batches/{date_str}/users/{public_user_id}/batch/{batch_id}",
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
            "url": url,
            "batch": data,
        }

    @r.get(
        "/latest-batch",
        summary="Get the latest batch (removed)",
        description=(
            "Removed - this returned the most recent submission to the now-removed "
            "`POST /batches/` endpoint, so it always returns 410 Gone."
        ),
    )
    def get_latest_batch(request):
        raise HttpError(410, "This endpoint has been removed along with POST /batches/.")

    @r.get(
        "/batches/{date_str}/users",
        summary="List crawlers active on a date",
        description=(
            "Return the list of public user ID hashes (SHA3-256) for all crawlers that submitted "
            "batches on the given date. `date_str` must be in `YYYY-MM-DD` format."
        ),
    )
    def get_user_id_hashes_for_date(request, date_str: str):
        check_date_str(date_str)
        prefix = f"1/{VERSION}/{date_str}/1/"
        return get_subfolders(prefix)

    @r.get(
        "/stats",
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
        "/",
        summary="Health check",
        description='Returns `{"status": "ok"}` if the crawler API is running.',
    )
    def status(request):
        return {"status": "ok"}

    class CuratedDomain(Schema):
        name: str

    class CuratedDomainsResponse(Schema):
        domains: list[CuratedDomain]

    @r.get(
        "/curated-domains",
        summary="Get curated domains for crawling",
        description=(
            "Retrieve the list of approved domains curated by mwmbl users. "
            "Crawlers should prioritize these domains when selecting URLs to crawl. "
            "Results are cached server-side for 5 minutes."
        ),
    )
    def get_curated_domains_endpoint(request) -> CuratedDomainsResponse:
        from mwmbl.curated_domains import get_curated_domains

        return CuratedDomainsResponse(domains=[CuratedDomain(name=d) for d in sorted(get_curated_domains())])

    @r.post(
        "/results",
        response={200: PostResultsResponse, 400: Error, 401: Error},
        summary="Submit indexed results",
        description=(
            "Submit a set of pre-indexed search results directly into the Mwmbl index. "
            "Requires a valid crawl-scoped API key passed in the `X-API-Key` request header "
            "(preferred) or in the request body `api_key` field (deprecated). "
            "Results are indexed immediately and also stored in object storage. "
            "This endpoint is intended for trusted crawlers.\n\n"
            "Pass `?dry_run=true` to authenticate and validate the request without indexing "
            "or storing anything. Useful for exercising the submission path from CI."
        ),
    )
    def post_results(request, results: Results, dry_run: bool = False):
        # Prefer X-API-Key header; fall back to deprecated body field
        raw_key = request.headers.get("X-API-Key") or results.api_key
        if not raw_key:
            return 401, {"message": "API key required. Pass it in the X-API-Key header."}

        api_key = _get_crawl_api_key(raw_key)
        if api_key is None:
            return 401, {"message": INVALID_CRAWL_KEY_MESSAGE}

        now = datetime.now(timezone.utc)
        ApiKey.objects.filter(pk=api_key.pk).update(last_used=now)
        now_ts = int(now.timestamp())

        documents = []
        for result in results.results:
            if result.last_crawled is not None and result.last_crawled > now_ts:
                return 400, {"message": f"last_crawled timestamp is in the future for URL: {result.url}"}
            last_crawled = result.last_crawled if result.last_crawled is not None else now_ts
            documents.append(
                Document(
                    url=result.url,
                    title=_truncated(result.title, NUM_TITLE_CHARS),
                    extract=_truncated(result.extract, NUM_EXTRACT_CHARS),
                    user_ids=[api_key.user.id],
                    last_crawled=last_crawled,
                )
            )

        if dry_run:
            return {"status": "dry-run", "url": None}

        # Update or create Device records for this submission. This used to happen on the
        # now-removed /batches/ endpoint, which was the only path that saw a device name.
        if results.device_name:
            record_device(api_key.user, results.device_name)

        index_path = f"{settings.DATA_PATH}/{settings.INDEX_NAME}"
        indexed = index_documents(documents, index_path)
        # Without api_key: the bucket is world-readable (get_batch_from_id fetches from it
        # with a plain unauthenticated GET), so uploading the object as submitted would
        # publish the plaintext key of any client still using the deprecated body field.
        filename = upload_object(results.model_copy(update={"api_key": None}), now, api_key.user.username, "results")

        # Update stats for the user
        stats_manager.record_results(results, api_key.user)

        # Last, since they are only analytics: a failure to store them still answers with an
        # error, but after the batch is indexed, uploaded and counted.
        record_agreements(indexed.agreements, api_key.user, results.crawler_version)
        worst_agreements.record_worst_agreements(
            worst_agreements.get_redis(), indexed.agreements, api_key.user.id, utc_today()
        )

        return {
            "status": "ok",
            "url": f"{PUBLIC_URL_PREFIX}{filename}",
        }

    @r.post(
        "/errors",
        response={200: StatusResponse, 401: Error},
        summary="Report a crawler error",
        description=(
            "Report an exception a crawler caught and recovered from. The crawler loops retry "
            "after every error, so without this a crawler can fail on every pass - and stop "
            "submitting results - with nothing to show for it but its own logs. Requires a "
            "crawl-scoped API key in the `X-API-Key` request header."
        ),
    )
    def post_error(request, report: CrawlerErrorReport):
        api_key = _get_crawl_api_key(request.headers.get("X-API-Key", ""))
        if api_key is None:
            return 401, {"message": INVALID_CRAWL_KEY_MESSAGE}

        # Info, not error: Sentry's logging integration sends every error log as an event of
        # its own, which would file each report a second time, under production.
        logger.info("Crawler error from user %s (%s): %s", api_key.user.id, report.context, report.exception_type)
        sentry_sdk.capture_event(_crawler_error_event(report, api_key.user.id))
        return {"status": "ok"}

    @r.post(
        "/dataset",
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
            raise HttpError(400, f"Incorrect user ID length, should be {USER_ID_LENGTH}")

        user_id_hash = _get_user_id_hash(dataset)

        # Create a hashed dataset that doesn't contain the raw user_id
        hashed_dataset = HashedDataset(
            user_id_hash=user_id_hash,
            date=dataset.date,
            timestamp=dataset.timestamp,
            extensionVersion=dataset.extensionVersion,
            queryDataset=dataset.queryDataset,
            searchResults=dataset.searchResults,
        )

        now = datetime.now(timezone.utc)
        filename = upload_object(hashed_dataset, now, user_id_hash, "dataset")

        # Record dataset statistics
        stats_manager.record_dataset(hashed_dataset)

        return {
            "status": "ok",
            "public_user_id": user_id_hash,
            "url": f"{PUBLIC_URL_PREFIX}{filename}",
        }


def init_router():
    """Initialise the module-level router (called from urls.py for the unified v1 API)."""
    _register_routes(router)


def create_router(version: str) -> NinjaAPI:
    """Create a standalone NinjaAPI for a specific version (used for legacy routes)."""
    api = NinjaAPI(urls_namespace=f"crawler-{version}")
    _register_routes(api)
    return api


def _get_crawl_api_key(raw_key: str) -> ApiKey | None:
    if not raw_key:
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return (
        ApiKey.objects.filter(
            key=key_hash,
            scopes__contains=[ApiKey.Scope.CRAWL],
        )
        .select_related("user")
        .first()
    )


def _crawler_error_event(report: CrawlerErrorReport, user_id: int) -> dict:
    """
    Build the Sentry event for a crawler's error as though Sentry had caught it itself.

    Sending the traceback as a structured exception rather than a message is what lets Sentry
    group reports by where they were raised, so a hundred crawlers hitting the same bug make
    one issue, not a hundred.
    """
    frames = [
        {
            "filename": frame.filename,
            "lineno": frame.lineno,
            "function": frame.function,
            "context_line": frame.line,
        }
        for frame in report.frames
    ]
    # Set explicitly, and never to None, or the SDK fills environment, release and server_name
    # in from the server itself, filing a volunteer's crawler bug under production on our host.
    # Its own environment puts crawler issues behind Sentry's environment filter, apart from ours.
    return {
        "level": "error",
        "logger": "mwmbl.crawler.remote",
        "environment": "crawler",
        "release": f"mwmbl-crawler@{report.crawler_version}",
        "server_name": report.device_name,
        "exception": {
            "values": [
                {
                    "type": report.exception_type,
                    "value": report.message,
                    "stacktrace": {"frames": frames},
                }
            ]
        },
        "tags": {
            "source": "crawler",
            "crawler_context": report.context,
            "crawler_version": report.crawler_version,
            "crawler_device": report.device_name,
        },
        "user": {"id": str(user_id)},
    }


def _get_user_id_hash(batch: DatasetRequest):
    return hashlib.sha3_256(batch.user_id.encode("utf8")).hexdigest()


def check_public_user_id(public_user_id):
    if len(public_user_id) != PUBLIC_USER_ID_LENGTH:
        raise HttpError(400, f"Incorrect public user ID length, should be {PUBLIC_USER_ID_LENGTH}")


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
    results = {"batch_ids": [get_batch_id_from_file_name(name) for name in filename_endings]}
    return results


def get_batches_for_prefix(prefix):
    s3 = boto3.resource(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    bucket = s3.Bucket(BUCKET_NAME)
    items = bucket.objects.filter(Prefix=prefix)
    filenames = [item.key for item in items]
    return filenames


def check_date_str(date_str):
    if not DATE_REGEX.match(date_str):
        raise HttpError(400, "Incorrect date format, should be YYYY-MM-DD")


def get_subfolders(prefix):
    client = boto3.client(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    items = client.list_objects(Bucket=BUCKET_NAME, Prefix=prefix, Delimiter="/")
    item_keys = [item["Prefix"][len(prefix) :].strip("/") for item in items["CommonPrefixes"]]
    return item_keys
