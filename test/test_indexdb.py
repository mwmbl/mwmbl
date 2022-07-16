from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase, clean_unicode
from mwmbl.tinysearchengine.indexer import Document


def test_bad_unicode_encoding():
    bad_doc = Document('Good title', 'https://goodurl.com', 'Bad extract text \ud83c', 1.0)
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.queue_documents([bad_doc])


def test_clean_unicode():
    result = clean_unicode('Bad extract text \ud83c')
    assert result == 'Bad extract text '
