from django.db import migrations

# The batches table tracked crawl batches through the REMOTE -> LOCAL -> URLS_UPDATED ->
# INDEXED pipeline that fed the index from object storage. Nothing has written to it since
# POST /crawler/batches/ was retired, and the code that read it (IndexDatabase, BatchCache,
# process_batch, historical) is gone, so the table is left over rather than in use.
#
# It was never a Django model - mwmbl.apps.create_index_db() issued the CREATE TABLE on
# every startup - so there is no model state to alter here, only raw SQL.
#
# The reverse recreates the table empty. The rows themselves are not recoverable, and they
# are not worth recovering: each one is a URL into object storage plus a status, and the
# batches those URLs point at are still in the bucket.


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0038_apikey_last_used_device"),
    ]

    operations = [
        migrations.RunSQL(
            "DROP TABLE IF EXISTS batches;",
            """
            CREATE TABLE IF NOT EXISTS batches (
                url VARCHAR PRIMARY KEY,
                user_id_hash VARCHAR NOT NULL,
                status INT NOT NULL
            );
            """,
        ),
    ]
