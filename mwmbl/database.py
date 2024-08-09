from psycopg2 import connect

from mwmbl import settings


class Database:
    def __init__(self):
        self.connection = None

    def __enter__(self):
        self.connection = connect(settings.DATABASE_URL)
        self.connection.set_session(autocommit=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.connection.close()
