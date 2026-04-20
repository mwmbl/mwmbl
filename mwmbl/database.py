from psycopg2 import connect

from django.conf import settings


def _get_database_url() -> str:
    """
    Derive a psycopg2 DSN from Django's DATABASES['default'] setting.
    Supports both a pre-built URL (via dj_database_url) and a dict of individual keys.
    """
    db = settings.DATABASES['default']
    # dj_database_url stores the original URL in the 'URL' key when available
    if 'URL' in db:
        return db['URL']
    # Fall back to constructing a DSN from individual keys
    host = db.get('HOST', 'localhost')
    port = db.get('PORT', '5432')
    name = db.get('NAME', '')
    user = db.get('USER', '')
    password = db.get('PASSWORD', '')
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


class Database:
    def __init__(self):
        self.connection = None

    def __enter__(self):
        self.connection = connect(_get_database_url())
        self.connection.set_session(autocommit=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.connection.close()
