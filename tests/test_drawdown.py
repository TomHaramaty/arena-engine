"""One wake per drawdown, not one per tick.

The bug these tests close was live and measured, not hypothetical. On
2026-07-30, tempo and vertex each woke 13 times between midnight and 08:00 UTC
— every tick of a pre-market night — against equity frozen to seven decimal
places (92744.32860671723, unchanged from 05:05 to 08:04). Each wake ran a brain
and appended a journal entry: 531 and 453 lines in one day's file, in a repo
whose first rule is that nothing can be edited or removed. $5.32 of model spend
before the market opened.

The cause was one word doing two jobs. `handled` on triggers_fired means "the
brain has seen this". mark_all filed a drawdown trigger unless an UNHANDLED one
existed, which read as "unless the agent is already dealing with it" — but the
flag is cleared the moment the session ends, and a session cannot put equity
back above the peak. So the guard released itself and the next tick re-filed the
same breach.
"""
from datetime import datetime, timedelta, timezone

from engine import core
from tests.fakedb import FakeConn

T0 = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)


def trigger(equity, peak=100_000.0, ts=T0, handled=True):
    return {"agent_id": "tempo", "kind": "drawdown", "handled": handled,
            "ts": ts, "details": {"equity": equity, "peak": peak}}


# ---------- the rule, in the pure ----------

def test_a_first_breach_always_wakes():
    assert core.drawdown_wake(None, 92_000.0, recovered=False) is True


def test_the_same_breach_does_not_wake_twice():
    """The whole bug: identical facts, already argued about."""
    last = {"equity": 92_744.33, "peak": 100_000.0}
    assert core.drawdown_wake(last, 92_744.33, recovered=False) is False


def test_drifting_deeper_without_a_full_step_does_not_wake():
    last = {"equity": 92_000.0, "peak": 100_000.0}
    assert core.drawdown_wake(last, 88_000.0, recovered=False) is False


def test_another_full_step_down_earns_a_new_wake():
    """A drawdown that deepens by another 7% is new information, and the agent
    is entitled to reconsider on it."""
    last = {"equity": 92_000.0, "peak": 100_000.0}
    assert core.drawdown_wake(last, 92_000.0 * (1 - core.DRAWDOWN_STEP),
                              recovered=False) is True


def test_a_recovery_ends_the_episode_and_the_next_breach_is_new():
    last = {"equity": 92_000.0, "peak": 100_000.0}
    assert core.drawdown_wake(last, 92_500.0, recovered=True) is True


def test_a_legacy_trigger_without_an_equity_holds_its_peace():
    """Triggers filed before 2026-07-30 carry no equity to compare against.
    Treat the episode as open rather than waking on an unknown."""
    assert core.drawdown_wake({"peak": 100_000.0}, 90_000.0,
                              recovered=False) is False


# ---------- the rule, against live state ----------

def test_unread_wake_is_never_stacked():
    """The agent has not deliberated yet — a second trigger adds nothing and
    would double-wake it the moment the first is handled."""
    c = FakeConn(triggers=[trigger(92_000.0, handled=False)])
    assert core.drawdown_due(c, "tempo", 80_000.0) is False


def test_recovery_is_read_from_the_marks_since_the_trigger():
    line = 100_000.0 * (1 - core.DRAWDOWN_TRIGGER)   # 93,000
    c = FakeConn(
        triggers=[trigger(92_000.0, ts=T0)],
        marks=[{"agent_id": "tempo", "ts": T0 + timedelta(hours=1),
                "equity": line + 500}],
    )
    assert core.drawdown_due(c, "tempo", 92_500.0) is True


def test_marks_that_never_cleared_the_line_are_not_a_recovery():
    c = FakeConn(
        triggers=[trigger(92_000.0, ts=T0)],
        marks=[{"agent_id": "tempo", "ts": T0 + timedelta(hours=1),
                "equity": 92_900.0}],       # still under 93,000
    )
    assert core.drawdown_due(c, "tempo", 92_500.0) is False


def test_a_mark_before_the_trigger_cannot_end_the_episode():
    c = FakeConn(
        triggers=[trigger(92_000.0, ts=T0)],
        marks=[{"agent_id": "tempo", "ts": T0 - timedelta(hours=1),
                "equity": 99_000.0}],
    )
    assert core.drawdown_due(c, "tempo", 92_000.0) is False


# ---------- the night that started this, walked tick by tick ----------

def marks_conn():
    """tempo as it stood: $100k peak, a position now worth 7.3% less, and
    nothing moving because the market is shut."""
    return FakeConn(agent_id="tempo", cash=0.0, peak=100_000.0,
                    positions={"AMD": {"qty": 1000.0, "avg_fill": 100.0}},
                    bench={"symbols": ["SPY"], "weights": [1.0],
                           "launch_prices": [500.0]})


def quotes(price=92.744, spy=500.0):
    return {"AMD": {"price": price}, "SPY": {"price": spy}}


def test_thirteen_ticks_of_a_frozen_night_wake_the_brain_once():
    c = marks_conn()
    for i in range(13):
        core.mark_all(c, quotes(), now=T0 + timedelta(hours=i))
    wakes = [t for t in c.triggers if t["kind"] == "drawdown"]
    assert len(wakes) == 1, f"woke {len(wakes)} times on identical facts"
    assert wakes[0]["details"]["equity"] == 92_744.0


def test_the_second_wake_needs_the_drawdown_to_deepen_by_a_step():
    c = marks_conn()
    core.mark_all(c, quotes(price=92.744), now=T0)
    for t in c.triggers:
        t["handled"] = True                       # the brain deliberated
    # a further 5% down: bad, but the same argument
    core.mark_all(c, quotes(price=88.0), now=T0 + timedelta(hours=1))
    assert len([t for t in c.triggers if t["kind"] == "drawdown"]) == 1
    # a full step below the level it last woke at: a new fact
    core.mark_all(c, quotes(price=86.0), now=T0 + timedelta(hours=2))
    assert len([t for t in c.triggers if t["kind"] == "drawdown"]) == 2


def test_a_recovery_then_a_new_breach_wakes_again():
    c = marks_conn()
    core.mark_all(c, quotes(price=92.0), now=T0)
    for t in c.triggers:
        t["handled"] = True
    core.mark_all(c, quotes(price=99.0), now=T0 + timedelta(hours=1))   # back up
    core.mark_all(c, quotes(price=91.0), now=T0 + timedelta(hours=2))   # down again
    assert len([t for t in c.triggers if t["kind"] == "drawdown"]) == 2


def test_marking_still_records_equity_every_tick():
    """Only the WAKE is rationed. The marks are the record of the drawdown and
    must keep landing on every tick."""
    c = marks_conn()
    for i in range(5):
        core.mark_all(c, quotes(), now=T0 + timedelta(hours=i))
    assert len(c.marks) == 5
