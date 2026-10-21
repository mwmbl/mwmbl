"""Upload a full backup of the search index to B2. See mwmbl.indexer.backup."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.indexer.backup import backup_index, backup_settings_from_environment


class Command(BaseCommand):
    help = "Upload a full, encrypted backup of the search index to B2"

    def add_arguments(self, parser):
        parser.add_argument("--index-name", default=settings.INDEX_NAME)

    def handle(self, *args, **options):
        client, bucket, encryption_key = backup_settings_from_environment()
        index_path = Path(settings.DATA_PATH) / options["index_name"]
        key = backup_index(client, bucket, encryption_key, index_path)
        self.stdout.write(f"Backed up {index_path} to {key}")
