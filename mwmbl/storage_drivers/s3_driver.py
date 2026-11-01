import boto3
from logging import getLogger
from mwmbl.settings import (
    APPLICATION_KEY,
    BUCKET_NAME,
    ENDPOINT_URL,
    KEY_ID,
)

logger = getLogger(__name__)

def _get_bucket(name):
    s3 = boto3.resource(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    return s3.Object(BUCKET_NAME, name)


def upload(data: bytes, name: str):
    logger.info(f"Uploading {len(data)} bytes to {name}")
    bucket = _get_bucket(name)
    bucket.put(Body=data)

def get_batches_for_prefix(prefix):
    s3 = boto3.resource(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    bucket = s3.Bucket(BUCKET_NAME)
    items = bucket.objects.filter(Prefix=prefix)
    filenames = [item.key for item in items]
    return filenames

def get_subfolders(prefix):
    client = boto3.client(
        "s3", endpoint_url=ENDPOINT_URL, aws_access_key_id=KEY_ID, aws_secret_access_key=APPLICATION_KEY
    )
    items = client.list_objects(Bucket=BUCKET_NAME, Prefix=prefix, Delimiter="/")
    item_keys = [item["Prefix"][len(prefix) :].strip("/") for item in items["CommonPrefixes"]]
    return item_keys
