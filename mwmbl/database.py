import os

from psycopg2 import connect


class Database:
    def __init__(self):
        self.connection = None

    def __enter__(self):
        self.connection = connect(os.environ["DATABASE_URL"])
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.connection.__exit__(exc_type, exc_val, exc_tb)
        self.connection.close()
