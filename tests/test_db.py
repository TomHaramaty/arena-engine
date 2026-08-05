"""The schema guard.

`db.migrate` ran on every job's connect, and its `alter table ... add column if
not exists` statements take ACCESS EXCLUSIVE whether or not they change
anything. Three times between 2026-08-02 and 08-03 an hourly tick took those
locks while a brain session was reading `watchlist`, and the session lost every
remaining operation in its batch to `error: deadlock detected`.

These tests defend the property that fixes it: in the steady state, migrate
issues no DDL at all.
"""
import hashlib
import pathlib

from engine import db

SCHEMA = (pathlib.Path(db.__file__).parent / "schema.sql").read_text()
DIGEST = hashlib.sha256(SCHEMA.encode()).hexdigest()


class Conn:
    """A connection that records statements and answers the two questions the
    guard asks."""

    def __init__(self, digest=None, has_table=True):
        self.digest = digest
        self.has_table = has_table
        self.statements = []
        self.commits = 0

    def execute(self, sql, args=None):
        self.statements.append(" ".join(str(sql).split()))
        if "to_regclass" in sql:
            return _rows([{"t": "schema_state" if self.has_table else None}])
        if "select digest from schema_state" in sql:
            return _rows([{"digest": self.digest}] if self.digest else [])
        return _rows([])

    def commit(self):
        self.commits += 1


class _rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def ddl(conn):
    return [s for s in conn.statements if "create table" in s or "alter table" in s]


def test_an_unchanged_schema_issues_no_ddl_and_takes_no_locks():
    """The whole point. This is the steady state — every job, every hour."""
    c = Conn(digest=DIGEST)
    db.migrate(c)
    assert ddl(c) == []
    assert c.commits == 0


def test_a_changed_schema_still_applies():
    c = Conn(digest="a digest from an older schema.sql")
    db.migrate(c)
    assert ddl(c), "a schema change must still reach the database"
    assert c.commits == 1


def test_a_database_that_has_never_been_migrated_gets_the_whole_schema():
    """Bootstrap: the table that records the digest does not exist yet, and
    asking it for one must not be how we find that out."""
    c = Conn(has_table=False)
    db.migrate(c)
    assert ddl(c)
    assert c.commits == 1


def test_the_applied_digest_is_recorded():
    c = Conn(has_table=False)
    db.migrate(c)
    assert any("insert into schema_state" in s for s in c.statements)


def test_the_second_run_after_a_change_is_quiet_again():
    first = Conn(digest="old")
    db.migrate(first)
    second = Conn(digest=DIGEST)
    db.migrate(second)
    assert ddl(second) == []
