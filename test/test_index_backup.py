"""Full backups of the index to B2, against moto's S3. See mwmbl.indexer.backup."""

import base64
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import boto3
import pytest
from botocore.exceptions import ResponseStreamingError
from django.core.management import call_command
from moto import mock_aws

from mwmbl.indexer import backup
from mwmbl.indexer.backup import LockedIndexReader, backup_index, backup_settings_from_environment, restore_index
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


def test_restore_reads_across_many_ranges(index_path, client, monkeypatch):
    # An odd size, so ranges end in the middle of zstd blocks.
    monkeypatch.setattr(backup, "DOWNLOAD_RANGE_SIZE", 1_000_003)
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)

    restored_path = index_path.with_name("restored.tinysearch")
    restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)

    assert restored_path.read_bytes() == index_path.read_bytes()


class _FailingBody:
    def read(self):
        raise ResponseStreamingError(error="connection reset")


def _fail_ranges(client, monkeypatch, failures: int):
    """Make the body of the first `failures` ranged GETs fail part-way through reading."""
    get_object = client.get_object
    remaining = [failures]

    def flaky_get_object(**kwargs):
        response = get_object(**kwargs)
        if "Range" in kwargs and remaining[0] > 0:
            remaining[0] -= 1
            response["Body"] = _FailingBody()
        return response

    monkeypatch.setattr(client, "get_object", flaky_get_object)
    monkeypatch.setattr(backup, "RANGE_RETRY_SECONDS", 0)


def test_restore_retries_a_range_whose_body_fails(index_path, client, monkeypatch):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    _fail_ranges(client, monkeypatch, failures=backup.RANGE_ATTEMPTS - 1)

    restored_path = index_path.with_name("restored.tinysearch")
    restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)

    assert restored_path.read_bytes() == index_path.read_bytes()


class _ShortBody:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body.read()[:-1]


def test_restore_retries_a_range_that_comes_back_short(index_path, client, monkeypatch):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    get_object = client.get_object
    shortened = []

    def short_get_object(**kwargs):
        response = get_object(**kwargs)
        if "Range" in kwargs and not shortened:
            shortened.append(kwargs["Range"])
            response["Body"] = _ShortBody(response["Body"])
        return response

    monkeypatch.setattr(client, "get_object", short_get_object)
    monkeypatch.setattr(backup, "RANGE_RETRY_SECONDS", 0)

    restored_path = index_path.with_name("restored.tinysearch")
    restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)

    assert shortened
    assert restored_path.read_bytes() == index_path.read_bytes()


def test_a_failed_restore_removes_what_it_wrote(index_path, client, monkeypatch):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    _fail_ranges(client, monkeypatch, failures=backup.RANGE_ATTEMPTS)

    restored_path = index_path.with_name("restored.tinysearch")
    with pytest.raises(ResponseStreamingError):
        restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)
    assert not restored_path.exists()
    assert not restored_path.with_name("restored.tinysearch.partial").exists()


def test_restore_writes_over_what_a_killed_restore_left(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    restored_path = index_path.with_name("restored.tinysearch")
    restored_path.with_name("restored.tinysearch.partial").write_bytes(b"left by a killed restore")

    restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)

    assert restored_path.read_bytes() == index_path.read_bytes()


def test_reading_an_index_shorter_than_its_metadata_says_fails(index_path):
    os.truncate(index_path, index_path.stat().st_size - PAGE_SIZE)
    reader = LockedIndexReader(index_path)

    with pytest.raises(EOFError):
        reader.read()
    reader.close()


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

    data_operations = {"CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload", "HeadObject"}
    data_calls = [call for call in calls if call[0] in data_operations]
    assert {name for name, _ in data_calls} == data_operations
    assert all(encryption_key == ENCRYPTION_KEY for _, encryption_key in data_calls)
    # Every GET but the one for the unencrypted manifest is a range of the backup.
    gets = [encryption_key for name, encryption_key in calls if name == "GetObject"]
    assert gets.count(None) == 1
    assert gets.count(ENCRYPTION_KEY) == len(gets) - 1 > 0


def test_a_restore_that_does_not_match_its_manifest_is_not_put_in_place(index_path, client):
    key = backup_index(client, BUCKET, ENCRYPTION_KEY, index_path)
    manifest = json.loads(client.get_object(Bucket=BUCKET, Key=key + ".json")["Body"].read())
    manifest["sha256"] = "0" * 64
    client.put_object(Bucket=BUCKET, Key=key + ".json", Body=json.dumps(manifest).encode("utf8"))

    restored_path = index_path.with_name("restored.tinysearch")
    with pytest.raises(ValueError, match="does not match its manifest"):
        restore_index(client, BUCKET, ENCRYPTION_KEY, key, restored_path)
    assert not restored_path.exists()
    assert not restored_path.with_name("restored.tinysearch.partial").exists()


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


@pytest.fixture
def backup_environment(monkeypatch, client):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # An AWS endpoint rather than B2's, so moto answers it.
    monkeypatch.setenv("MWMBL_BACKUP_ENDPOINT_URL", "https://s3.us-east-1.amazonaws.com")
    monkeypatch.setenv("MWMBL_BACKUP_KEY_ID", "testing")
    monkeypatch.setenv("MWMBL_BACKUP_APPLICATION_KEY", "testing")
    monkeypatch.setenv("MWMBL_BACKUP_BUCKET", BUCKET)
    monkeypatch.setenv("MWMBL_BACKUP_ENCRYPTION_KEY", base64.b64encode(ENCRYPTION_KEY).decode("ascii"))


def test_backup_settings_come_from_the_environment(backup_environment):
    _, bucket, encryption_key = backup_settings_from_environment()

    assert bucket == BUCKET
    assert encryption_key == ENCRYPTION_KEY


def test_an_encryption_key_of_the_wrong_length_is_refused(backup_environment, monkeypatch):
    monkeypatch.setenv("MWMBL_BACKUP_ENCRYPTION_KEY", base64.b64encode(bytes(16)).decode("ascii"))

    with pytest.raises(ValueError, match="must be 32 bytes"):
        backup_settings_from_environment()


def test_the_commands_back_up_and_restore_the_index(index_path, backup_environment, settings, capsys):
    settings.DATA_PATH = str(index_path.parent)

    call_command("backup_index", "--index-name", index_path.name)
    key = capsys.readouterr().out.strip().rsplit(" ", 1)[-1]
    restored_path = index_path.with_name("restored.tinysearch")
    call_command("restore_index", key, str(restored_path))

    assert f"Restored {key} to {restored_path}" in capsys.readouterr().out
    assert restored_path.read_bytes() == index_path.read_bytes()
