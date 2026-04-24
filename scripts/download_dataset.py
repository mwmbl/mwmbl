import boto3
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor


load_dotenv()


# --- Configuration ---
ENDPOINT_URL = os.environ["MWMBL_ENDPOINT_URL"]
BUCKET_NAME = os.environ["MWMBL_BUCKET_NAME"]
BB_KEY_ID = os.environ["MWMBL_KEY_ID"]
BB_APPLICATION_KEY = os.environ["MWMBL_APPLICATION_KEY"]
VERSION = 'v1'
OBJECT_TYPE = 'dataset'


# Initialize B2 via Boto3
s3 = boto3.client(
    's3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=BB_KEY_ID,
    aws_secret_access_key=BB_APPLICATION_KEY,
)

def download_file(s3_key):
    """Downloads a single object to a local path mimicking the S3 structure."""
    local_path = os.path.join('downloads', s3_key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    
    print(f"Downloading: {s3_key}")
    s3.download_file(BUCKET_NAME, s3_key, local_path)

def get_files_for_date(target_date):
    """Lists all files for a specific date prefix."""
    prefix = f'1/{VERSION}/{target_date}/{OBJECT_TYPE}/'
    print("Looking for files with prefix", prefix)
    paginator = s3.get_paginator('list_objects_v2')
    
    keys = []
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
        if 'Contents' in page:
            for obj in page['Contents']:
                keys.append(obj['Key'])
    return keys

def sync_date_range(start_str, end_str):
    """Main orchestrator for the date range."""
    start = datetime.strptime(start_str, '%Y-%m-%d')
    end = datetime.strptime(end_str, '%Y-%m-%d')
    
    current = start
    all_keys = []
    
    # 1. Gather all keys for the range
    print(f"Scanning for files between {start_str} and {end_str}...")
    while current <= end:
        date_str = current.strftime('%Y-%m-%d')
        all_keys.extend(get_files_for_date(date_str))
        current += timedelta(days=1)
    
    # 2. Parallel Download
    print(f"Found {len(all_keys)} files. Starting parallel download...")
    with ThreadPoolExecutor(max_workers=20) as executor:
        executor.map(download_file, all_keys)

if __name__ == "__main__":
    sync_date_range('2026-03-01', '2026-04-03')


