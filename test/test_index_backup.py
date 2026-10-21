"""Full backups of the index to B2, against moto's S3. See mwmbl.indexer.backup."""

import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import boto3
import pytest
from moto import mock_aws

from mwmbl.indexer import backup
from mwmbl.indexer.backup import LockedIndexReader, backup_index, restore_index
from mwmbl.tinysearchengine.indexer import METADATA_SIZE, Document, TinyIndex

NUM_PAGES = 4000
PAGE_SIZE = 4096
BUCKET = "index-backups"
ENCRYPTION_KEY = bytes(range(32))


@pytest.fixture
def index_path():
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "index.tinysearch"
        TinyIndex.create(Document, str(path), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
        with TinyIndex(Document, path, "w") as index:
            with index.page(3) as page:
                page.store([Document("Title", "https://example.com/", "Extract")])
        # Incompressible bytes over most of the file, so the upload really is multipart.
        with open(path, "r+b") as index_file:
            index_file.seek(METADATA_SIZE + 100 * PAGE_SIZE)
            index_file.write(os.urandom(3000 * PAGE_SIZE))
        yield path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    # S3's minimum part size, so the ~16 MB test index goes up in several parts.
    monkeypatch.setattr(backup, "MIN_PART_SIZE", 5 * 1024 * 1024)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def test_restore_gives_back_the_index_byte_for_byte(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)

    restored_path = index_path.with_name("restored.tinysearch")
    restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)

    assert restored_path.read_bytes() == index_path.read_bytes()
    with TinyIndex(Document, restored_path) as restored:
        assert [document.url for document in restored.get_page(3)] == ["https://example.com/"]


def test_manifest_describes_the_backup(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)

    manifest = json.loads(client.get_object(Bucket=BUCKET, Key=key + ".json")["Body"].read())
    assert manifest["num_pages"] == NUM_PAGES
    assert manifest["page_size"] == PAGE_SIZE
    assert manifest["size"] == index_path.stat().st_size


def test_every_request_for_the_backup_carries_the_encryption_key(index_path, client):
    """moto does not enforce SSE-C, so check the key goes on every request that touches the data."""
    calls = []

    def record(params, model, **kwargs):
        calls.append((model.name, params.get("SSECustomerKey")))

    client.meta.events.register("provide-client-params.s3.*", record)
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    restore_index(client, BUCKET, ENCRYPTION_KEY, key, index_path.with_name("restored.tinysearch"))

    data_operations = {"CreateMultipartUpload", "UploadPart", "HeadObject"}
    data_calls = [call for call in calls if call[0] in data_operations]
    assert {name for name, _ in data_calls} == data_operations
    assert all(encryption_key == ENCRYPTION_KEY for _, encryption_key in data_calls)
    ranged_gets = [call for call in calls if call[0] == "GetObject" and call[1] is not None]
    assert ranged_gets


def test_a_restore_that_does_not_match_its_manifest_is_not_put_in_place(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    manifest = json.loads(client.get_object(Bucket=BUCKET, Key=key + ".json")["Body"].read())
    manifest["sha256"] = "0" * 64
    client.put_object(Bucket=BUCKET, Key=key + ".json", Body=json.dumps(manifest).encode("utf8"))

    restored_path = index_path.with_name("restored.tinysearch")
    with pytest.raises(ValueError, match="does not match its manifest"):
        restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)
    assert not restored_path.exists()


def test_restore_will_not_overwrite_an_existing_file(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)

    with pytest.raises(FileExistsError):
        restore_index(client, BUCKET, ENCRYPTION_KEY, key, index_path)


def test_reading_waits_for_a_writer_holding_a_page(index_path):
    """A page caught half-written would never decode again, so the reader must wait for it."""
    reader = LockedIndexReader(index_path)
    finished = threading.Event()

    def read_everything():
        reader.read()
        finished.set()

    with TinyIndex(Document, index_path, "w") as index:
        with index.page(NUM_PAGES - 1):
            thread = threading.Thread(target=read_everything)
            thread.start()
            assert not finished.wait(timeout=0.5), "the reader read a page a writer was holding"
        thread.join(timeout=10)
    reader.close()

    assert finished.is_set()
