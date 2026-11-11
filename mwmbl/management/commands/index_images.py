"""Write the approved Commons files to the search index, up to IMAGE_INDEX_LIMIT a run, so it can run on a schedule:

    uv run manage.py index_images

Does nothing unless IMAGE_INDEXING_ENABLED is set.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.imagesearch.indexing import index_images


class Command(BaseCommand):
    help = "Write approved Commons images to the search index"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=settings.IMAGE_INDEX_LIMIT)

    def handle(self, *args, **options):
        if not settings.IMAGE_INDEXING_ENABLED:
            self.stdout.write("Image indexing is off: set IMAGE_INDEXING_ENABLED=true to write images to the index")
            return
        index_path = str(Path(settings.DATA_PATH) / settings.INDEX_NAME)
        counts = index_images(index_path, options["limit"])
        for outcome, count in sorted(counts.items()):
            self.stdout.write(f"{outcome}: {count}")
