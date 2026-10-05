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


def _clock(hour, minute, day=30, month=7):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)
    return Fixed


# ---------- the late backup ----------

@pytest.mark.parametrize("when", [
    (0, 12, 1, 10),    # Thu 2026-10-01: the 20:50 backup ran Thursday's open overnight
    (19, 34, 2, 10),   # Fri 2026-10-02: the 14:50 backup ran the close before the bell
    (0, 1, 3, 10),     # Sat 2026-10-03: a full session on a Saturday
    (14, 40, 4, 10),   # Sun: no bell at all
])
def test_a_late_backup_runs_nobody(when, ran, monkeypatch, capsys):
    hour, minute, day, month = when
    conn = StubConn(due=["tempo"])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "")
    monkeypatch.setattr(dispatch, "datetime", _clock(hour, minute, day, month))
    dispatch.dispatch(triggers_only=False)
    assert ran == []
    assert conn.asked == []
    assert "late backup; not running" in capsys.readouterr().out


@pytest.mark.parametrize("hour, minute, slot", [
    (14, 40, "open"), (15, 40, "open"), (20, 40, "close"), (21, 40, "close"),
])
def test_the_bells_still_ring_on_a_weekday(hour, minute, slot, ran, monkeypatch, capsys):
    """The primaries send a blank slot too, so the windows must hold both bells,
    before and after US daylight time ends."""
    conn = StubConn(due=["tempo"])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "")
    monkeypatch.setattr(dispatch, "datetime", _clock(hour, minute, 5, 10))
    dispatch.dispatch(triggers_only=False)
    assert ran == [("tempo", "scheduled")]
    assert f"daily slot: {slot}" in capsys.readouterr().out


def test_an_explicit_slot_is_an_operators_catch_up_and_is_never_refused(ran, monkeypatch):
    conn = StubConn(due=["tempo"])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "open")
    monkeypatch.setattr(dispatch, "datetime", _clock(18, 0, 3, 10))
    dispatch.dispatch(triggers_only=False)
    assert ran == [("tempo", "scheduled")]


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


# ---------- how loudly a failure is allowed to be reported ----------
#
# Every scheduled dispatch between 2026-08-03 and 2026-08-13 went red on three
# to six brains out of thirty-five, so the day GitHub's git operations went down
# and no brain ran at all looked exactly like the eighteen days before it.

def _floor(n):
    return [chr(ord("a") + i) for i in range(n)]


def _fails(*ids):
    def run(_conn, aid, trigger):
        if aid in ids:
            raise RuntimeError("the operations block is not valid json")
    return run


def test_a_few_flaky_brains_do_not_make_the_slot_a_failure(monkeypatch, capsys):
    """Thirty-two of thirty-five sessions happened. That is the slot working."""
    conn = StubConn(due=_floor(35))
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "open")
    monkeypatch.setattr(dispatch, "run_agent", _fails("a", "b", "c"))
    dispatch.dispatch(triggers_only=False)   # green
    out = capsys.readouterr().out
    assert "dispatch done: 32/35 succeeded" in out
    assert "within the tolerance of 3" in out


def test_a_slot_losing_more_than_a_tenth_of_the_floor_is_a_failure(monkeypatch):
    conn = StubConn(due=_floor(35))
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "open")
    monkeypatch.setattr(dispatch, "run_agent", _fails("a", "b", "c", "d"))
    with pytest.raises(SystemExit):
        dispatch.dispatch(triggers_only=False)


def test_a_tolerated_failure_is_still_reported(monkeypatch):
    """Green must never mean unnoticed: the exception the loop absorbs is the
    only trace that agent lost its session."""
    seen = []
    conn = StubConn(due=_floor(35))
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "open")
    monkeypatch.setattr(dispatch, "run_agent", _fails("a"))
    monkeypatch.setattr(dispatch.obs, "capture",
                        lambda exc, **tags: seen.append(tags))
    dispatch.dispatch(triggers_only=False)
    assert seen == [{"agent": "a", "trigger": "scheduled"}]


def test_the_one_agent_woken_by_an_event_still_fails_the_step(monkeypatch):
    """A tenth of one is none: tolerance scales with the floor and never
    silences a dispatch small enough for the failure to be the whole of it."""
    conn = StubConn(due=["tempo"])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setattr(dispatch, "run_agent", _fails("tempo"))
    with pytest.raises(SystemExit):
        dispatch.dispatch(triggers_only=True)


# ---------- the guard that decides whether a slot may be given back ----------

def test_a_session_that_never_touched_the_book_is_not_counted_as_run(monkeypatch):
    """The backup trigger exists for exactly this: the primary's parse failures
    are the agents most owed a session, and until 2026-08-13 they were the ones
    it skipped."""
    conn = StubConn(due=[])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    monkeypatch.setenv("DAILY_SLOT", "open")
    dispatch.dispatch(triggers_only=False)
    sql, _ = conn.asked[0]
    assert "r.status = 'failed'" in sql
    assert "coalesce((r.meta->>'retryable')::boolean, false)" in sql


def test_a_quiet_arena_does_nothing_and_says_so(monkeypatch, capsys):
    conn = StubConn(due=[])
    monkeypatch.setattr(dispatch.db, "connect", lambda: conn)
    dispatch.dispatch(triggers_only=True)
    assert "no agents due" in capsys.readouterr().out
