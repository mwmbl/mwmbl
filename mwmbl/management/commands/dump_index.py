"""Upload a full dump of the search index to B2. See mwmbl.indexer.dump."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.dump import dump_index, dump_settings_from_environment


class Command(BaseCommand):
    help = "Upload a full dump of the search index to B2"

    def add_arguments(self, parser):
        parser.add_argument("--index-name", default=settings.INDEX_NAME)

    def handle(self, *args, **options):
        client, bucket = dump_settings_from_environment()
        index_path = Path(settings.DATA_PATH) / options["index_name"]
        key = dump_index(client, bucket, index_path)
        self.stdout.write(f"Dumped {index_path} to {key}")
