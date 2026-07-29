"""Range-aware standing-order fills (2026-07-29).

The tick samples the market roughly hourly; measured on the record, 37% of a
session's price range fell between samples and fired stops executed an average
1.19% past their own trigger. These tests pin the fix: orders fire when the
session's high/low touched them since the last look, at the trigger (or the
open on a gap) — and never on an extreme printed before the engine first saw
the order.
"""
from datetime import datetime, timezone

from engine import core


def sess(date="2026-07-29", high=110.0, low=90.0, open_=100.0):
    return {"date": date, "high": high, "low": low, "open": open_}


# ---------- the no-hindsight baseline ----------

def test_first_observation_stamps_baseline_and_holds_fire():
    # The session low is already past the trigger — but it printed before the
    # engine ever saw this order. Nothing may fire on it.
    params = {"trigger_price": 95.0}
    basis, baseline = core.range_fire("stop", "sell", params, sess(low=90.0))
    assert basis is None
    assert baseline == {"seen_session": "2026-07-29",
                        "seen_high": 110.0, "seen_low": 90.0}


def test_same_session_stale_extreme_does_not_refire():
    # Extremes unchanged since the last look → no new information, no fire,
    # even though the session low sits below the trigger.
    params = {"trigger_price": 95.0, "seen_session": "2026-07-29",
              "seen_high": 110.0, "seen_low": 90.0}
    basis, _ = core.range_fire("stop", "sell", params, sess(low=90.0))
    assert basis is None


def test_same_session_new_low_through_trigger_fills_at_trigger():
    params = {"trigger_price": 95.0, "seen_session": "2026-07-29",
              "seen_high": 110.0, "seen_low": 96.0}
    basis, _ = core.range_fire("stop", "sell", params, sess(low=94.0))
    assert basis == 95.0  # at the trigger, not at whatever price we sampled


def test_same_session_new_low_short_of_trigger_holds():
    params = {"trigger_price": 93.0, "seen_session": "2026-07-29",
              "seen_high": 110.0, "seen_low": 96.0}
    basis, _ = core.range_fire("stop", "sell", params, sess(low=94.0))
    assert basis is None


# ---------- a new session for an order that lived through the close ----------

def test_new_session_gap_through_fills_at_open():
    # The market opened below the stop: the honest fill is the open.
    params = {"trigger_price": 95.0, "seen_session": "2026-07-28",
              "seen_high": 110.0, "seen_low": 96.0}
    basis, baseline = core.range_fire(
        "stop", "sell", params, sess(open_=92.0, low=90.0))
    assert basis == 92.0
    assert baseline["seen_session"] == "2026-07-29"


def test_new_session_intraday_touch_fills_at_trigger():
    params = {"trigger_price": 95.0, "seen_session": "2026-07-28",
              "seen_high": 110.0, "seen_low": 96.0}
    basis, _ = core.range_fire("stop", "sell", params, sess(open_=100.0, low=94.0))
    assert basis == 95.0


# ---------- the other three shapes ----------

def test_limit_buy_gap_down_fills_at_the_better_open():
    # A limit buy met by a gap down fills at the open — price improvement,
    # exactly as a real limit order behaves.
    params = {"limit_price": 95.0, "seen_session": "2026-07-28",
              "seen_high": 110.0, "seen_low": 96.0}
    basis, _ = core.range_fire("limit", "buy", params, sess(open_=92.0, low=91.0))
    assert basis == 92.0


def test_limit_sell_new_high_fires_at_the_limit():
    params = {"limit_price": 108.0, "seen_session": "2026-07-29",
              "seen_high": 105.0, "seen_low": 96.0}
    basis, _ = core.range_fire("limit", "sell", params, sess(high=109.0))
    assert basis == 108.0


def test_stop_buy_gap_up_fills_at_open():
    params = {"trigger_price": 105.0, "seen_session": "2026-07-28",
              "seen_high": 104.0, "seen_low": 96.0}
    basis, _ = core.range_fire("stop", "buy", params, sess(open_=107.0, high=112.0))
    assert basis == 107.0


def test_trailing_touch_is_judged_against_the_stored_water():
    # trig = 100 * (1 - 0.10) = 90 from the water as stored — never from a
    # water raised by a high printed in the same unobserved window.
    params = {"trail_pct": 0.10, "high_water": 100.0,
              "seen_session": "2026-07-29", "seen_high": 100.0, "seen_low": 95.0}
    basis, _ = core.range_fire(
        "trailing_stop", "sell", params, sess(high=200.0, low=89.0))
    assert basis == 90.0


# ---------- what the range must never be used for ----------

def test_session_of_rejects_crypto_and_partial_payloads():
    ts = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
    crypto = {"price": 64000.0, "high": 65000.0, "low": 62000.0,
              "open": 63000.0, "ts": ts, "session_range": False}
    assert core.session_of(crypto) is None
    partial = {"price": 100.0, "high": None, "low": 90.0, "open": 100.0,
               "ts": ts, "session_range": True}
    assert core.session_of(partial) is None
    good = {"price": 100.0, "high": 110.0, "low": 90.0, "open": 100.0,
            "ts": ts, "session_range": True}
    assert core.session_of(good) == {"date": "2026-07-29", "high": 110.0,
                                     "low": 90.0, "open": 100.0}


# ---------- the whole path, walked against a fake connection ----------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.log.append((" ".join(sql.split()), args))
        s = sql.strip().lower()
        if s.startswith("select * from orders"):
            self._rows = [dict(o) for o in self.conn.orders]
        elif s.startswith("select id, config from agents"):
            self._rows = [{"id": "tempo", "config": {}}]
        elif s.startswith("select qty from positions"):
            self._rows = [{"qty": self.conn.position_qty}]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Just enough connection to walk evaluate_standing_orders: routes the
    reads, records every write. Param updates land back on the order so a
    second tick sees what the first one stamped — the property under test."""

    def __init__(self, orders, position_qty=10.0):
        self.orders = orders
        self.position_qty = position_qty
        self.log = []

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, args=None):
        self.log.append((" ".join(sql.split()), args))
        s = sql.strip().lower()
        if s.startswith("update orders set params"):
            import json
            merged, oid = json.loads(args[0]), args[1]
            for o in self.orders:
                if o["id"] == oid:
                    o["params"] = {**(o["params"] or {}), **merged}

    def commit(self):
        pass

    def writes(self, prefix):
        return [(s, a) for s, a in self.log if s.lower().startswith(prefix)]


def quote(price, high, low, open_, day=29):
    return {"price": price, "high": high, "low": low, "open": open_,
            "prev_close": None, "session_range": True,
            "ts": datetime(2026, 7, day, 15, 0, tzinfo=timezone.utc)}


def test_two_ticks_stamp_then_fire_at_the_trigger():
    order = {"id": 7, "agent_id": "tempo", "kind": "stop", "side": "sell",
             "symbol": "AMD", "qty": None, "params": {"trigger_price": 95.0}}
    conn = FakeConn([order])

    # Tick 1: the session low is already below the trigger, but it predates
    # the engine seeing the order — baseline stamped, nothing fires (the last
    # price is above the trigger, so the fallback holds too).
    filled = core.evaluate_standing_orders(
        conn, {"AMD": quote(price=98.0, high=110.0, low=94.0, open_=100.0)})
    assert filled == []
    assert order["params"]["seen_session"] == "2026-07-29"
    assert order["params"]["seen_low"] == 94.0

    # Tick 2: a NEW low through the trigger, while the sampled price has
    # already bounced back above it — the old code would have missed this
    # entirely. It fills at the trigger.
    filled = core.evaluate_standing_orders(
        conn, {"AMD": quote(price=97.0, high=110.0, low=93.0, open_=100.0)})
    assert len(filled) == 1
    _, basis, qty = filled[0]
    assert basis == 95.0 and qty == 10.0

    fill_writes = conn.writes("insert into fills")
    assert len(fill_writes) == 1
    # values (order_id, agent_id, symbol, qty, price, fill_price, ts)
    args = fill_writes[0][1]
    assert args[4] == 95.0                                # at the trigger
    assert abs(args[5] - core.sell_fill_price(95.0)) < 1e-9

    trig_writes = conn.writes("insert into triggers_fired")
    assert len(trig_writes) == 1
    import json
    details = json.loads(trig_writes[0][1][1])
    assert details["via"] == "range" and details["trigger_price"] == 95.0


def test_trailing_water_never_rises_on_prints_the_engine_had_not_seen():
    # First meeting: the session high (120) predates the order — the water
    # must rise only to the sampled price, not to the high.
    order = {"id": 9, "agent_id": "tempo", "kind": "trailing_stop",
             "side": "sell", "symbol": "GS", "qty": None,
             "params": {"trail_pct": 0.10, "high_water": 100.0}}
    conn = FakeConn([order])
    core.evaluate_standing_orders(
        conn, {"GS": quote(price=104.0, high=120.0, low=99.0, open_=100.0)})
    assert order["params"]["high_water"] == 104.0

    # Same session, a NEW high beyond the baseline: live knowledge, it counts.
    core.evaluate_standing_orders(
        conn, {"GS": quote(price=118.0, high=125.0, low=99.0, open_=100.0)})
    assert order["params"]["high_water"] == 125.0
