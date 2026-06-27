import os

import psycopg
from psycopg.rows import dict_row

DEFAULT_URL = "postgresql://opendrop:opendrop@db:5432/opendrop"


def connect():
    url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    return psycopg.connect(url, row_factory=dict_row)
