import os
import pathlib

import psycopg
from psycopg.rows import dict_row


def connect():
    url = os.environ["DATABASE_URL"]
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=20)


def migrate(conn):
    sql = (pathlib.Path(__file__).parent / "schema.sql").read_text()
    conn.execute(sql)
    conn.commit()
