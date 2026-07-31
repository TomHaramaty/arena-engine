"""What a rejected applicant is told, and whether they can recover.

The seat clears the interview the moment an application is written, so a
rejection is the one moment where fifteen minutes of somebody's work is held
only by the application doc. `blocked` is the field the seat reads to decide
whether it may offer a rename and resubmit, so it has to be right: too eager
and the principal is invited to fix something a rename cannot fix, too shy and
a recoverable charter looks dead.
"""

import copy

import pytest

from engine import seating
from jobs import ingest
from tests.test_seating import PACKET


class FakeRef:
    def __init__(self):
        self.written = None

    def update(self, data):
        self.written = data


class FakeDoc:
    def __init__(self, data, doc_id="app1"):
        self.id, self._data = doc_id, data
        self.reference = FakeRef()

    def to_dict(self):
        return self._data


class FakeConn:
    """Only the reads a rejection makes. An unknown query is a bug, not a
    shrug, so anything else raises."""

    def __init__(self, taken=(), live_uids=(), symbols=("SPY",)):
        self.taken, self.live, self.symbols = list(taken), list(live_uids), list(symbols)
        self._last = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("select 1 from agents where id="):
            self._last = []  # the resume path: this uid has not been seated
        elif s.startswith("select id from agents"):
            self._last = [{"id": i} for i in self.taken]
        elif s.startswith("select owner_uid from agents"):
            self._last = [{"owner_uid": u} for u in self.live]
        elif s.startswith("select symbol from watchlist"):
            self._last = [{"symbol": s_} for s_ in self.symbols]
        else:
            raise AssertionError(f"unexpected sql: {s}")
        return self

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


@pytest.fixture(autouse=True)
def _no_trader_repo(tmp_path, monkeypatch):
    """taken_ids also reads the trader repo's agents/ directory; point it at an
    empty one so the database is the only source of taken names here."""
    monkeypatch.setattr(ingest, "TRADER", tmp_path)


def reject(packet, taken=(), **kw):
    doc = FakeDoc({"uid": "u1", "packet": packet})
    conn = FakeConn(taken=taken, **kw)
    ingest.process(conn, None, doc, __import__("datetime").date(2026, 7, 31))
    return doc.reference.written


def test_a_name_collision_is_marked_recoverable():
    """The whole point: losing a race for a word must cost the word, not the
    charter, and the seat can only offer that if it is told."""
    written = reject(dict(copy.deepcopy(PACKET), name="vector"), taken=["vector"])
    assert written["status"] == "rejected"
    assert written["blocked"] == ["name"]
    assert "already registered" in written["reasons"][0]


def test_a_reserved_name_is_recoverable_too():
    written = reject(dict(copy.deepcopy(PACKET), name="spy"), taken=[])
    assert written["blocked"] == ["name"]


def test_a_rejection_that_is_not_only_the_name_is_not_marked_recoverable():
    """A rename would not fix this one, so the seat must not offer it."""
    packet = dict(copy.deepcopy(PACKET), name="vector", max_position_pct=99)
    written = reject(packet, taken=["vector"])
    assert len(written["reasons"]) > 1
    assert written["blocked"] == []


def test_a_rejection_with_nothing_wrong_with_the_name_carries_no_code():
    packet = dict(copy.deepcopy(PACKET), max_position_pct=99)
    written = reject(packet)
    assert written["blocked"] == []
    assert written["reasons"]


def test_the_reasons_are_still_written_verbatim():
    """They are the Registrar's words to the principal; `blocked` is only a
    hint to the page and never replaces them."""
    written = reject(dict(copy.deepcopy(PACKET), name="vector"), taken=["vector"])
    assert written["reasons"] == [seating.check_name("vector", {"vector"})]
