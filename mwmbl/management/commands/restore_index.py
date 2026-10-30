"""Download a full dump of the search index from B2. See mwmbl.indexer.dump."""

from pathlib import Path

from django.core.management.base import BaseCommand

from mwmbl.indexer.dump import dump_settings_from_environment, restore_index


class Command(BaseCommand):
    help = "Download a full dump of the search index from B2 and check it against its manifest"

    def add_arguments(self, parser):
        parser.add_argument("key", help="The dump object's key, as dump_index printed it")
        parser.add_argument("output_path", type=Path, help="Where to write the index; must not exist")

    def handle(self, *args, **options):
        client, bucket = dump_settings_from_environment()
        restore_index(client, bucket, options["key"], options["output_path"])
        self.stdout.write(f"Restored {options['key']} to {options['output_path']}")
