import pathlib
import time

import pyarrow.parquet as pq
from pydantic import StrictStr, validator

from mwmbl.pipeline.connections.connection_catalog import PyarrowS3FSConnection, S3FSConnection
from mwmbl.pipeline.connections.connection_group_handler import get_global_connections_handler
from mwmbl.pipeline.messages.std_data import StdData
from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel


class DownloadCCIndexModel(BaseOpModel):
    s3fs_conn_name: StrictStr
    pyarrow_s3fs_conn_name: StrictStr
    local_dest_dir: StrictStr
    s3_src_dir: StrictStr
    skip_existing: bool = True
    glob_pattern: StrictStr = "**/*.parquet"

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False

    @validator("local_dest_dir")
    def validate_local_dest_dir(cls, local_dest_dir):
        """Validate.

        * Make sure local_test_dir is a directory
        """
        if not pathlib.Path(local_dest_dir).is_dir():
            raise ValueError(f"{local_dest_dir=} is not a directory.")
        else:
            return local_dest_dir


class DownloadCCIndex(BaseOp):
    OP_TYPE = "commoncrawl_download_cc_index"
    OP_MODEL = DownloadCCIndexModel

    def __init__(
            self,
            s3fs_conn_name: str,
            pyarrow_s3fs_conn_name: str,
            local_dest_dir: str,
            s3_src_dir: str,
            skip_existing: bool = True,
            glob_pattern: str = "**/*.parquet",
    ):
        """Initialize.

        * We use s3fs instead of pas3fs for listdir operations since it is more user-friendly.
        * We use pas3fs instead of s3fs for downloading and writing to file since ParquetDataset
          and write_to_dataset performs better.
        * Once downloaded, the `table` object consumes upto 8 GB of memory, it is recommended to
          have around 10 GB of unused memory when using DownloadCCIndex.
        * Although the file (with subset of 5 columns) is only ~400-700 MB on s3/disk, it can
          consume about 6-8 GB when decompressed from gzip.
        * We download `url_host_name` column as well to help with filtering the urls later.

        Args:
            s3fs_conn_name (str): The name/id of the s3fs_conn Connection in the
                connections_handler.
            pyarrow_s3fs_conn_name (str): The name/id of the pyarrow_s3fs_conn Connection in the
                connections_handler.
            local_dest_dir (str): The abosolute/relative path to the directory on the local disk
                where the files must be downloaded.
                - It must already exist.
            s3_src_dir (str): The full path/prefix of the s3 directory where the files need to be
                downloaded from. Example: "commoncrawl/cc-index/table/cc-main/warc/crawl=CC-MAIN-2021-43/subset=warc"
                - You can choose other `crawl` partitions.
                - This Op can only handle the `warc` parition.
            skip_existing (bool): Default True. If True, the filenames that already exist in the
                local_dest_dir are not downloaded from s3.
            glob_pattern (str): The pattern of files to look for on s3.
                - This need not be changed from its default value if using commoncrawl bucket.
                - This Op can only handle parquet files.
        """
        self.connections_handler = get_global_connections_handler()

        s3fs_conn: S3FSConnection = self.connections_handler.get_conn(conn_name=s3fs_conn_name)
        self.s3fs_fs = s3fs_conn.fs
        pas3fs_conn: PyarrowS3FSConnection = self.connections_handler.get_conn(
            conn_name=pyarrow_s3fs_conn_name
        )
        self.pas3fs_fs = pas3fs_conn.fs

        self.s3_src_dir = s3_src_dir
        self.local_dest_dir = pathlib.Path(local_dest_dir)
        self.skip_existing = skip_existing
        self.glob_pattern = glob_pattern

    def run(self, data: StdData) -> StdData:
        """Run the Op."""
        print("DownloadCCIndex: Running")

        # Get the s3 filepaths using s3fs. .
        s3_filepaths = [metadata['Key'] for metadata in self.s3fs_fs.listdir(self.s3_src_dir)]
        print(f"DownloadCCIndex: {len(s3_filepaths)=}")

        if self.skip_existing:
            print(f"DownloadCCIndex: Skipping download of files that already exist in local dir")
            existing_filenames = set(
                [path.name for path in self.local_dest_dir.glob(self.glob_pattern)]
            )
            print(f"DownloadCCIndex: {len(existing_filenames)=}")
            filtered_s3_filepaths = [
                filepath
                for filepath in s3_filepaths
                if pathlib.Path(filepath).name not in existing_filenames
            ]
            print(f"DownloadCCIndex: {len(filtered_s3_filepaths)=}")
        else:
            filtered_s3_filepaths = s3_filepaths

        for i, s3_filepath in enumerate(filtered_s3_filepaths):
            filename = pathlib.Path(s3_filepath).name
            print(f"\tTime taken: {i=:04d}: {filename=:25s}: ", end="", flush=True)

            start = time.time()
            # TODO: Optimize downloading/streaming of s3 file straight to disk instead of
            # decompressing it in memory. This might be difficult due to parquet file metadata
            # not being complete until the entire file has been read into memory or written to disk.
            table = pq.ParquetDataset(
                path_or_paths=s3_filepath,
                use_legacy_dataset=False,
                filesystem=self.pas3fs_fs,
            ).read_pandas(columns=[
                'url', 'warc_filename', 'warc_record_offset', 'warc_record_length', 'url_host_name'
            ])
            end = time.time()
            print(f"to download: {end - start:05.2f} | ", end="", flush=True)

            start = time.time()
            pq.write_to_dataset(
                table,
                root_path=str(self.local_dest_dir),
                # We `use_legacy_dataset=True` otherwise we cannot use `partition_filename_cb`
                use_legacy_dataset=True,
                # Ignore partition keys etc., just use the original filename, otherwise
                # random filenames are generated which would make it difficult to keep track of
                # which files have been downloaded.
                partition_filename_cb=lambda x: filename,
                # Use DictionaryEncoding for certain columns which have categorical data
                # This makes writing faster, reduces disk consumption (by a small amount) and could
                # potentially make reads faster (not always).
                use_dictionary=['url_host_name', 'warc_filename'],
            )
            end = time.time()
            print(f"to write: {end - start:05.2f}")

            # Delete table object which could be upto 8 GB of memory
            # This statement does seem to make memory consumption more predictable
            del table

        # Return dummy data for now.
        return StdData(data=None)
