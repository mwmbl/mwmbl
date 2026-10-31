# Index dumps

A dump is a full copy of the search index, uploaded to a public B2 bucket on the first of
every month (the `cron` entry in `app.json`). It serves as both the index's backup and its
public release. The index is the only copy of much of what it holds (seed crawls, curation,
Staan copies). It is also open data: `/page/{n}` already serves it one page at a time, and
a dump holds the same data as a single download. See `mwmbl/indexer/dump.py` for how.

Like everything Mwmbl's crawlers and contributors provide, the dumps are released under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) (see the terms and
conditions), and each manifest says so.

## Setup

1. Create a public B2 bucket for the dumps, separate from `mwmbl-eu-crawl`. Every dump is
   a new file, so retention is a lifecycle rule on the `index/` prefix rather than on old
   versions:

   ```json
   {"fileNamePrefix": "index/", "daysFromUploadingToHiding": 100, "daysFromHidingToDeleting": 1,
    "daysFromStartingToCancelingUnfinishedLargeFiles": 7}
   ```

   That keeps the last three monthly dumps. It also cleans up uploads that failed
   part-way, which the dump's own key cannot delete.

   Enable Object Lock on the bucket, with a default retention of 90 days in governance
   mode. The dump's key already cannot delete files. The lock also protects the dumps from
   a mistake made with any key that can delete. The retention has to end before the
   lifecycle rule deletes, at 101 days, or the rule cannot delete them and old dumps pile
   up. Governance mode lets a key with `bypassGovernance`, such as the master key, still
   remove a bad dump. Compliance mode would also survive a compromised B2 account, but
   then nothing can remove a locked file early. Every dump and manifest is a new file
   name, so the lock never blocks an upload.

   B2's free egress is three times the data stored, about 3.6 TB a month for three
   ~400 GB dumps, which covers roughly nine full downloads. If downloads outgrow that,
   put a CDN in B2's Bandwidth Alliance, such as bunny.net or Fastly, in front of the
   bucket. Egress to those is free.
2. Create an application key restricted to that bucket, without `deleteFiles`, so a
   compromised server cannot remove the dumps. B2's lifecycle rules do the deleting.
3. Set the environment:

   ```sh
   dokku config:set mwmbl \
     MWMBL_DUMP_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com \
     MWMBL_DUMP_BUCKET=<bucket> \
     MWMBL_DUMP_KEY_ID=<key id> \
     MWMBL_DUMP_APPLICATION_KEY=<application key>
   ```

4. Run the first dump by hand rather than waiting for the cron:
   `dokku run mwmbl ionice -c2 -n7 /venv/bin/python /app/manage.py dump_index`.
   Once it has finished, restore it somewhere with room to spare (below).

The command checks in with Sentry Crons as the `dump-index` monitor, which Sentry creates
on the first check-in. Sentry alerts if a dump fails, runs past a day (killed part-way),
or doesn't start within an hour of the schedule. If you change the cron schedule in
`app.json`, change `MONITOR_CONFIG` in `mwmbl/management/commands/dump_index.py` to match;
a test checks that the two agree.

A dump object is `index/<index name>/full/<start time>.zst`. Its manifest,
`<object>.json`, is written only once the upload has finished, so an object without one
is an upload that failed part-way.

## Restore

The restore needs as much free disk as the index, about 420 GB.

```sh
dokku run mwmbl /venv/bin/python /app/manage.py restore_index \
  index/index-v2-400G.tinysearch/full/<start time>.zst /app/storage/restored.tinysearch
```

It refuses to overwrite an existing file, and checks the result against the manifest's
size and SHA-256 before renaming it into place. It writes to `<output>.partial` until then:
a restore that fails removes it, but one that was killed leaves it, up to 420 GB of the
live volume. Delete it, or restore to the same path again, which writes over it. Then stop the app, move the restored file
over `/app/storage/index-v2-400G.tinysearch`, and start it again.
