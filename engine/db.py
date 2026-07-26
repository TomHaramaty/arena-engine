import os
import pathlib
import time

import psycopg
from psycopg.rows import dict_row


def connect(attempts=4):
    """Connect to Postgres, retrying transient failures with backoff.

    Neon serverless can cold-start or briefly refuse connections
    (OperationalError: "connection is bad" / "network is unreachable"), which
    otherwise aborts a whole job at startup — the exact transient behind the
    tick failure on 2026-07-26. A few backed-off retries ride that out; a real
    outage still raises after the last attempt.
    """
    url = os.environ["DATABASE_URL"]
    delay = 1.0
    for attempt in range(attempts):
        try:
            return psycopg.connect(url, row_factory=dict_row, connect_timeout=20)
        except psycopg.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def migrate(conn):
    sql = (pathlib.Path(__file__).parent / "schema.sql").read_text()
    conn.execute(sql)
    conn.commit()
