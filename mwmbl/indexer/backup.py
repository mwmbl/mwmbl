"""Full backups of the search index to B2.

The index is the only copy of much of what it holds - seed crawls, curation, Staan copies
- so rebuilding it from the crawl batches would recover only part of it. A full backup
streams the whole file, zstd-compressed, into one object, and a manifest written beside
it once the upload has finished records its size and SHA-256. An object with no manifest
is an upload that did not finish.

The file is written to in place all the time, and a page caught half-written will never
decode again, so it is read in chunks under a shared lock on each chunk's byte range.
Writers take an exclusive lock on the page they change (TinyIndex.page), so every page in
the backup is one that some writer actually stored. Pages are independent, so consistency
across the file is not needed.

The objects are encrypted with SSE-C: B2 holds the data but not the key, which is kept off
the host. Losing the key loses the backups.
"""

import base64
import fcntl
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path

import boto3
import zstandard
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from mwmbl.tinysearchengine.indexer import METADATA_SIZE, Document, TinyIndex, _set_page_lock

logger = getLogger(__name__)

PAGES_PER_CHUNK = 1024
# S3 allows at most 10,000 parts in a multipart upload. The part size is chosen from the
# file size so a full index stays well inside that, with headroom for an index that grows.
MAX_PARTS = 9000
MIN_PART_SIZE = 64 * 1024 * 1024
UPLOAD_CONCURRENCY = 4
# Restore downloads in ranges, each its own request, so a dropped connection hours into a
# 400 GB download costs one range's retries rather than the whole restore.
DOWNLOAD_RANGE_SIZE = 64 * 1024 * 1024
ZSTD_LEVEL = 3
SSE_ALGORITHM = "AES256"


def backup_settings_from_environment():
    """The B2 client, bucket and encryption key for backups.

    Read from the environment when a backup runs rather than in settings, so that a server
    with backups not yet configured still starts. The backup key is its own, separate from
    the crawl bucket's, and should be one that cannot delete files.
    """
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["MWMBL_BACKUP_ENDPOINT_URL"],
        aws_access_key_id=os.environ["MWMBL_BACKUP_KEY_ID"],
        aws_secret_access_key=os.environ["MWMBL_BACKUP_APPLICATION_KEY"],
        config=Config(retries={"mode": "standard", "max_attempts": 10}),
    )
    # 32 random bytes, base64-encoded: `openssl rand -base64 32`.
    encryption_key = base64.b64decode(os.environ["MWMBL_BACKUP_ENCRYPTION_KEY"], validate=True)
    if len(encryption_key) != 32:
        raise ValueError(f"MWMBL_BACKUP_ENCRYPTION_KEY must be 32 bytes, base64-encoded; it is {len(encryption_key)}")
    return client, os.environ["MWMBL_BACKUP_BUCKET"], encryption_key


class LockedIndexReader:
    """A file-like view of the index, read a chunk at a time under a shared page lock.

    It also hashes what it reads, so the manifest describes exactly the bytes uploaded.
    """

    def __init__(self, index_path: Path):
        index = TinyIndex(item_factory=Document, index_path=index_path)
        self.page_size = index.page_size
        self.num_pages = index.num_pages
        self.size = METADATA_SIZE + self.num_pages * self.page_size
        self.sha256 = hashlib.sha256()
        self._file = open(index_path, "rb")
        self._position = 0
        self._chunk = b""
        self._chunk_offset = 0

    def close(self):
        self._file.close()

    def read(self, size: int = -1) -> bytes:
        # zstd asks for ~128 KB at a time out of 4 MB chunks, so slice the chunk at an
        # offset rather than re-copying what is left of it on every read.
        parts = []
        remaining = self.size if size < 0 else size
        while remaining > 0:
            if self._chunk_offset == len(self._chunk):
                if self._position == self.size:
                    break
                self._chunk = self._read_next_chunk()
                self._chunk_offset = 0
            part = self._chunk[self._chunk_offset : self._chunk_offset + remaining]
            self._chunk_offset += len(part)
            remaining -= len(part)
            parts.append(part)
        return b"".join(parts)

    def _read_next_chunk(self) -> bytes:
        # The metadata is written once, when the index is created, and has no lock of its own.
        if self._position == 0:
            chunk = os.pread(self._file.fileno(), METADATA_SIZE, 0)
        else:
            length = min(PAGES_PER_CHUNK * self.page_size, self.size - self._position)
            fileno = self._file.fileno()
            _set_page_lock(fileno, fcntl.F_RDLCK, self._position, length)
            try:
                chunk = os.pread(fileno, length, self._position)
            finally:
                _set_page_lock(fileno, fcntl.F_UNLCK, self._position, length)
        if len(chunk) == 0:
            raise EOFError(f"The index ended at {self._position} bytes, but its metadata says {self.size}")
        self._position += len(chunk)
        self.sha256.update(chunk)
        return chunk


def backup_index(client, bucket: str, encryption_key: bytes, index_path: Path) -> str:
    """Upload a full backup of the index, returning the key of the backup object."""
    started = datetime.now(timezone.utc)
    key = f"index/{index_path.name}/full/{started:%Y-%m-%dT%H%M%SZ}.zst"
    reader = LockedIndexReader(index_path)
    part_size = max(MIN_PART_SIZE, math.ceil(reader.size / MAX_PARTS))
    transfer_config = TransferConfig(multipart_chunksize=part_size, max_concurrency=UPLOAD_CONCURRENCY)
    logger.info(f"Backing up {reader.size} bytes of {index_path} to {key} in parts of {part_size} bytes")
    try:
        compressed = zstandard.ZstdCompressor(level=ZSTD_LEVEL).stream_reader(reader)
        client.upload_fileobj(
            compressed, bucket, key, ExtraArgs=_encryption_args(encryption_key), Config=transfer_config
        )
    finally:
        reader.close()

    manifest = {
        "index_name": index_path.name,
        "page_size": reader.page_size,
        "num_pages": reader.num_pages,
        "size": reader.size,
        "sha256": reader.sha256.hexdigest(),
        "started": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
    }
    client.put_object(Bucket=bucket, Key=_manifest_key(key), Body=json.dumps(manifest).encode("utf8"))
    logger.info(f"Backed up {index_path} to {key}")
    return key


class _RangedObjectReader:
    """A file-like view of an object, fetched a range at a time."""

    def __init__(self, client, bucket: str, key: str, encryption_key: bytes):
        self._client = client
        self._bucket = bucket
        self._key = key
        self._encryption_key = encryption_key
        head = client.head_object(Bucket=bucket, Key=key, **_encryption_args(encryption_key))
        self._size = head["ContentLength"]
        self._position = 0

    def read(self, size: int) -> bytes:
        if self._position >= self._size:
            return b""
        end = min(self._position + max(size, DOWNLOAD_RANGE_SIZE), self._size) - 1
        response = self._client.get_object(
            Bucket=self._bucket,
            Key=self._key,
            Range=f"bytes={self._position}-{end}",
            **_encryption_args(self._encryption_key),
        )
        data = response["Body"].read()
        self._position += len(data)
        return data


def restore_index(client, bucket: str, encryption_key: bytes, key: str, output_path: Path):
    """Download a full backup to output_path, checking it against its manifest.

    It is written beside output_path and renamed into place only once it checks out, so a
    restore that fails leaves nothing that looks like an index.
    """
    if output_path.exists():
        raise FileExistsError(f"{output_path} already exists; restore to a new path")
    manifest = json.loads(client.get_object(Bucket=bucket, Key=_manifest_key(key))["Body"].read())
    partial_path = output_path.with_name(output_path.name + ".partial")
    sha256 = hashlib.sha256()
    size = 0
    source = _RangedObjectReader(client, bucket, key, encryption_key)
    decompressed = zstandard.ZstdDecompressor().stream_reader(source)
    with open(partial_path, "xb") as output_file:
        while chunk := decompressed.read(DOWNLOAD_RANGE_SIZE):
            sha256.update(chunk)
            size += len(chunk)
            output_file.write(chunk)

    if size != manifest["size"] or sha256.hexdigest() != manifest["sha256"]:
        raise ValueError(
            f"The restored index does not match its manifest: {size} bytes with SHA-256 "
            f"{sha256.hexdigest()}, expected {manifest['size']} bytes with {manifest['sha256']}. "
            f"It is left at {partial_path}."
        )
    partial_path.rename(output_path)
    logger.info(f"Restored {key} to {output_path}")


def _manifest_key(key: str) -> str:
    return key + ".json"


def _encryption_args(encryption_key: bytes) -> dict:
    # boto3 base64-encodes the key and adds its MD5 itself.
    return {"SSECustomerAlgorithm": SSE_ALGORITHM, "SSECustomerKey": encryption_key}
