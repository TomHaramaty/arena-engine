"""Which agents run, and how often they may.

The slot logic and the daily ceiling are the two things here that decide whether
a market session happens at all, so they are tested against a stub connection
rather than left to production to discover.
"""
from datetime import datetime, timezone

import pytest

from jobs import dispatch


class StubConn:
    """Answers the two queries dispatch asks, and records what was asked."""

    def __init__(self, due=(), sessions_today=None):
        self.due = list(due)
        self.sessions_today = dict(sessions_today or {})
        self.asked = []
        self.rolled_back = 0

    def execute(self, sql, args=None):
        s = " ".join(sql.split()).lower()
        self.asked.append((s, args))
        if "count(*) n from runs" in s:
            limit = args[0]
            return _Rows([{"agent_id": a, "n": n}
                          for a, n in sorted(self.sessions_today.items())
                          if n >= limit])
        if "from agents a" in s or "from agents" in s:
            return _Rows([{"id": a} for a in self.due])
        raise AssertionError(f"unexpected query: {sql}")

    def rollback(self):
        self.rolled_back += 1


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


@pytest.fixture
def ran(monkeypatch):
    """Record which agents were actually run, without running one."""
    seen = []
    monkeypatch.setattr(dispatch, "run_agent",
                        lambda conn, aid, trigger: seen.append((aid, trigger)))
    return seen


# ---------- the slot ----------

def test_the_slot_is_taken_from_the_workflow_when_it_says(monkeypatch):
    monkeypatch.setenv("DAILY_SLOT", "close")
    assert dispatch.daily_slot() == "close"


def test_a_blank_slot_is_inferred_from_the_clock(monkeypatch):
    """Schedule events carry no inputs, so the hour decides: the bells are at
    14:40 and 20:40 UTC."""
    monkeypatch.setenv("DAILY_SLOT", "")
    monkeypatch.setattr(dispatch, "datetime", _clock(14, 40))
    assert dispatch.daily_slot() == "open"
    monkeypatch.setattr(dispatch, "datetime", _clock(20, 40))
    assert dispatch.daily_slot() == "close"


def test_a_nonsense_slot_falls_back_to_the_clock(monkeypatch):
    monkeypatch.setenv("DAILY_SLOT", "brunch")
    monkeypatch.setattr(dispatch, "datetime", _clock(14, 40))
    assert dispatch.daily_slot() == "open"


def _clock(hour, minute):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 30, hour, minute, tzinfo=timezone.utc)
    return Fixed


# ---------- the ceiling ----------

def test_an_ordinary_days_sessions_are_under_the_ceiling():
    assert dispatch.over_ceiling(StubConn(sessions_today={"tempo": 3})) == {}


def test_an_agent_over_the_ceiling_is_reported():
    conn = StubConn(sessions_today={"vertex": 13, "tempo": 2})
    assert dispatch.over_ceiling(conn) == {"vertex": 13}


def test_the_ceiling_leaves_room_for_the_bells_and_real_events():
    """Two scheduled sessions plus four events is a busy, legitimate day."""
    assert dispatch.MAX_EVENT_SESSIONS_PER_DAY == 6
    conn = StubConn(sessions_today={"tempo": 5})
    assert dispatch.over_ceiling(conn) == {}


def test_the_loop_that_started_this_would_have_been_stopped():
    """tempo and vertex ran 13 event sessions each in one night."""
    conn = StubConn(sessions_today={"tempo": 13, "vertex": 13})
    assert set(dispatch.over_ceiling(conn)) == {"tempo", "vertex"}


def test_the_ceiling_is_asked_with_the_limit_it_enforces():
    conn = StubConn(sessions_today={})
    dispatch.over_ceiling(conn)
    _, args = conn.asked[0]
    assert args == (dispatch.MAX_EVENT_SESSIONS_PER_DAY,)


# ---------- event dispatch, end to end over the stub ----------

def test_event_dispatch_skips_the_agents_at_their_ceiling(ran, monkeypatch):
    conn = StubConn(due=["tempo", "vertex", "rapid"],
                    sessions_today={"tempo": 9, "vertex": 13})
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    dispatch.dispatch(triggers_only=True)
    assert ran == [("rapid", "event")]


def test_event_dispatch_says_out_loud_which_agents_it_held(ran, monkeypatch, capsys):
    conn = StubConn(due=["vertex"], sessions_today={"vertex": 13})
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    dispatch.dispatch(triggers_only=True)
    out = capsys.readouterr().out
    assert "CEILING: 13 sessions today" in out
    assert "triggering in a loop" in out
    assert ran == []


def test_one_agents_failure_never_blocks_the_others(monkeypatch):
    conn = StubConn(due=["a", "b", "c"])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    done = []

    def flaky(_conn, aid, trigger):
        if aid == "b":
            raise RuntimeError("the model refused")
        done.append(aid)

    monkeypatch.setattr(dispatch, "run_agent", flaky)
    with pytest.raises(SystemExit):          # the job still reports failure
        dispatch.dispatch(triggers_only=True)
    assert done == ["a", "c"]
    assert conn.rolled_back == 1


def test_a_quiet_arena_does_nothing_and_says_so(monkeypatch, capsys):
    conn = StubConn(due=[])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    dispatch.dispatch(triggers_only=True)
    assert "no agents due" in capsys.readouterr().out
