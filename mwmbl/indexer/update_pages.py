"""
Iterate over each page in the index and update it based on what is in the index database.
"""
import traceback
from time import sleep

from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


def run_update(index_path):
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()

    with TinyIndex(Document, index_path, 'w') as indexer:
        for i in range(indexer.num_pages):
            with Database() as db:
                index_db = IndexDatabase(db.connection)
                pages = index_db.get_queued_documents_for_page(i)
                if len(pages) > 0:
                    print("Pages", len(pages))
                else:
                    continue

                for j in range(3):
                    try:
                        indexer.add_to_page(i, pages)
                        break
                    except ValueError:
                        pages = pages[:len(pages)//2]
                        if len(pages) == 0:
                            break
                        print(f"Not enough space, adding {len(pages)}")
                index_db.clear_queued_documents_for_page(i)


def run(index_path):
    while True:
        try:
            run_update(index_path)
        except Exception as e:
            print("Exception updating pages in index")
            traceback.print_exception(type(e), e, e.__traceback__)
            sleep(10)


if __name__ == '__main__':
    run_update('data/index.tinysearch')
