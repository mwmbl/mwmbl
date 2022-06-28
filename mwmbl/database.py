import os

from psycopg2 import connect


class Database:
    def __init__(self):
        self.connection = None

    def __enter__(self):
        self.connection = connect(os.environ["DATABASE_URL"])
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.connection.close()
