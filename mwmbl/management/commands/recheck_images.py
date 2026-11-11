"""Recheck the stored Commons files checked longest ago, revoking those that no longer pass.

Bounded by IMAGE_STORE_RECHECK_LIMIT per run, so it can run on a schedule and work through the store over time:

    uv run manage.py recheck_images
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.imagesearch.recheck import recheck


class Command(BaseCommand):
    help = "Recheck stored Commons images against Commons and the NSFW judge"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=settings.IMAGE_STORE_RECHECK_LIMIT)

    def handle(self, *args, **options):
        outcomes = recheck(options["limit"])
        for outcome, count in sorted(outcomes.items()):
            self.stdout.write(f"{outcome}: {count}")
