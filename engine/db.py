import hashlib
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
    """Apply schema.sql, but only when it has changed since it was last applied.

    Every job called this on connect. schema.sql is twenty
    `create table if not exists` and nine `alter table ... add column if not
    exists`, and "if not exists" makes a statement a no-op — it does not make
    it lock-free. ALTER TABLE takes ACCESS EXCLUSIVE on its table whether or
    not it changes anything, and this file takes them on eight tables inside
    one transaction.

    So an hourly tick starting while a brain session held the ACCESS SHARE its
    `select 1 from watchlist` needs deadlocked the session: three times between
    2026-08-02 and 2026-08-03, and because a deadlock aborts the transaction,
    each one cost that agent EVERY remaining operation in its batch — fifteen
    refusals from three collisions, all of them reported to the agent, and one
    of them to its principal, as though the arena had decided something.

    The digest guard makes the steady state a single SELECT against a one-row
    table and no locks at all. A changed schema.sql applies exactly as before.

    The trade this accepts: migrate no longer repairs a schema that drifted
    without schema.sql changing (a table dropped by hand, say). Recording what
    was applied is worth more than re-asserting it hourly.
    """
    sql = (pathlib.Path(__file__).parent / "schema.sql").read_text()
    digest = hashlib.sha256(sql.encode()).hexdigest()
    if _schema_digest(conn) == digest:
        return
    conn.execute(sql)
    conn.execute(
        """insert into schema_state (id, digest) values (1, %s)
           on conflict (id) do update set digest=excluded.digest, applied_at=now()""",
        (digest,),
    )
    conn.commit()


def _schema_digest(conn):
    """The schema.sql this database last had applied, or None — including on a
    database so new that the table recording it does not exist yet."""
    if not conn.execute(
            "select to_regclass('schema_state') as t").fetchone()["t"]:
        return None
    row = conn.execute("select digest from schema_state where id=1").fetchone()
    return row["digest"] if row else None
