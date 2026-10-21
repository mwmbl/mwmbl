# Index backups

The search index is the only copy of much of what it holds - seed crawls, curation, Staan
copies - so it is backed up whole to a B2 bucket of its own, on the first of every month
(the `cron` entry in `app.json`). See `mwmbl/indexer/backup.py` for how.

## Setup

1. Create a private B2 bucket for the backups, separate from `mwmbl-eu-crawl`. Give it a
   lifecycle rule that keeps old versions for as long as you want backups kept.
2. Create an application key restricted to that bucket, without `deleteFiles`, so a
   compromised server cannot remove the backups.
3. Generate an encryption key with `openssl rand -base64 32`. Keep a copy somewhere other
   than the server: B2 never sees it, and without it the backups cannot be read.
4. Set the environment:

   ```sh
   dokku config:set mwmbl \
     MWMBL_BACKUP_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com \
     MWMBL_BACKUP_BUCKET=<bucket> \
     MWMBL_BACKUP_KEY_ID=<key id> \
     MWMBL_BACKUP_APPLICATION_KEY=<application key> \
     MWMBL_BACKUP_ENCRYPTION_KEY=<base64 key>
   ```

5. Run the first backup by hand rather than waiting for the cron:
   `dokku run mwmbl ionice -c3 /venv/bin/python /app/manage.py backup_index`.

A backup object is `index/<index name>/full/<start time>.zst`. Its manifest,
`<object>.json`, is written only once the upload has finished, so an object without one
is an upload that failed part-way.

## Restore

The restore needs as much free disk as the index, about 420 GB.

```sh
dokku run mwmbl /venv/bin/python /app/manage.py restore_index \
  index/index-v2-400G.tinysearch/full/<start time>.zst /app/storage/restored.tinysearch
```

It refuses to overwrite an existing file, and checks the result against the manifest's
size and SHA-256 before renaming it into place. Then stop the app, move the restored file
over `/app/storage/index-v2-400G.tinysearch`, and start it again.
