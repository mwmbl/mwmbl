"""
Iterate over each page in the index and update it based on what is in the index database.
"""
from mwmbl.database import Database
from mwmbl.indexdb import IndexDatabase
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


def run(index_path):
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


if __name__ == '__main__':
    run('data/index.tinysearch')
