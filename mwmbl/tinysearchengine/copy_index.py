"""
Copy an old index into a new one
"""
from pathlib import Path

from django.conf import settings

from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.models import OldIndex
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


NUM_PAGES_TO_COPY = 10


def copy_pages():
    new_index_path = Path(settings.DATA_PATH) / INDEX_NAME

    with TinyIndex(item_factory=Document, index_path=new_index_path) as new_index:
        # Get all old indexes:
        old_indexes = OldIndex.objects.all()
        for old_index_info in old_indexes:
            old_index_path = Path(old_index.index_path)
            with TinyIndex(item_factory=Document, index_path=old_index_path) as old_index:
                # Copy each page in the old index into the new one
                start_page = old_index_info.start_page
                for i in range(start_page, start_page + NUM_PAGES_TO_COPY):

                    # TODO: merge the items in the new and old indexes
                    page = old_index.get_page(i)
                    new_index.add_page(page)
