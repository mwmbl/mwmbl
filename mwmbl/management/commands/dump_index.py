"""Upload a full dump of the search index to B2. See mwmbl.indexer.dump."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from sentry_sdk.crons import monitor

from mwmbl.indexer.dump import dump_index, dump_settings_from_environment

# Sentry already reports an exception the dump raises. The check-ins also catch a dump that
# never reports back: one killed part-way, or a cron that didn't run. The schedule must
# match the cron entry in app.json.
MONITOR_SLUG = "dump-index"
MONITOR_CONFIG = {
    "schedule": {"type": "crontab", "value": "17 3 1 * *"},
    "timezone": "UTC",
    "checkin_margin": 60,
    # A dump of the ~400 GB index takes hours; a day is well past any that is still working.
    "max_runtime": 24 * 60,
}


class Command(BaseCommand):
    help = "Upload a full dump of the search index to B2"

    def add_arguments(self, parser):
        parser.add_argument("--index-name", default=settings.INDEX_NAME)

    def handle(self, *args, **options):
        client, bucket = dump_settings_from_environment()
        index_path = Path(settings.DATA_PATH) / options["index_name"]
        with monitor(monitor_slug=MONITOR_SLUG, monitor_config=MONITOR_CONFIG):
            key = dump_index(client, bucket, index_path)
        self.stdout.write(f"Dumped {index_path} to {key}")
