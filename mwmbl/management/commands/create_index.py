"""Create the search index if it is missing, and check an existing one is the right shape.

    uv run manage.py create_index --settings=mwmbl.settings_dev

The server never creates the index itself (see MwmblConfig.ready), so run this once per
new environment. Running it again on an existing index only checks it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from mwmbl.apps import create_index


class Command(BaseCommand):
    help = "Create the search index at DATA_PATH/INDEX_NAME if it does not exist"

    def handle(self, *args, **options):
        create_index(settings.INDEX_NAME, settings.NUM_PAGES)
