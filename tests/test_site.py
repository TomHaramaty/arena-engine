"""The tape: every action the floor took, and the words the record actually
holds for it. Also the line the public artifact must not cross — run costs and
token counts belong to the operator, not to arena.json."""
from datetime import datetime, timezone

from jobs import site


def T(day, hour=15, minute=0):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


class FakeConn:
    """Dispatches on the shape of the SQL, so the tests read as data not as
    query strings. Anything unmatched comes back empty."""

    def __init__(self, theses=(), fills=(), orders=(), rejected=()):
        self.sets = {"theses": list(theses), "fills": list(fills),
                     "orders": list(orders), "rejected": list(rejected)}

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "from fills f" in s:
            self._rows = self.sets["fills"]
        elif "from orders where kind <> 'market'" in s:
            self._rows = self.sets["orders"]
        elif "o.verdict='rejected'" in s:
            self._rows = self.sets["rejected"]
        elif "o.verdict='accepted'" in s:
            self._rows = self.sets["theses"]
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# ---- the price a resting rule fires at ----------------------------------

def test_trigger_prices_match_the_engine():
    assert site.order_trigger("stop", {"trigger_price": 315.0}) == 315.0
    assert site.order_trigger("limit", {"limit_price": 129.71}) == 129.71
    # engine/core.py: hw * (1 - trail_pct)
    assert site.order_trigger(
        "trailing_stop", {"high_water": 1222.11, "trail_pct": 0.1}) == 1099.899


def test_trigger_is_none_rather_than_wrong():
    """A malformed or unknown rule prints nothing. It never guesses a price the
    floor would then show a visitor as a real level."""
    assert site.order_trigger("stop", {}) is None
    assert site.order_trigger("trailing_stop", {"high_water": 10}) is None
    assert site.order_trigger("market", {"trigger_price": 5}) is None
    assert site.order_trigger("stop", None) is None


# ---- the tape ------------------------------------------------------------

def test_market_buy_is_given_its_thesis_from_its_own_run():
    """orders has no reason column for market orders. The buy's words come from
    the accepted operation of the same run — same run, same symbol."""
    conn = FakeConn(
        theses=[{"run_id": 7, "payload": {"symbol": "AMD", "thesis": "22% off the high"}}],
        fills=[{"ts": T(28), "agent_id": "maverick", "symbol": "AMD", "side": "buy",
                "qty": 53.9, "fill_price": 456.17, "kind": "market", "reason": None,
                "run_id": 7}],
    )
    (ev,) = site.tape_block(conn)
    assert ev["event"] == "fill" and ev["mechanism"] == "market"
    assert ev["note"] == "22% off the high"
    assert ev["price"] == 456.17 and ev["agent"] == "maverick"


def test_a_thesis_from_another_run_is_not_borrowed():
    conn = FakeConn(
        theses=[{"run_id": 7, "payload": {"symbol": "AMD", "thesis": "22% off the high"}}],
        fills=[{"ts": T(28), "agent_id": "maverick", "symbol": "AMD", "side": "buy",
                "qty": 1, "fill_price": 456.17, "kind": "market", "reason": None,
                "run_id": 99}],
    )
    (ev,) = site.tape_block(conn)
    assert ev["note"] == ""


def test_a_stop_fill_carries_the_note_it_was_armed_with():
    conn = FakeConn(fills=[
        {"ts": T(27), "agent_id": "tempo", "symbol": "AMD", "side": "sell", "qty": 44.7,
         "fill_price": 486.01, "kind": "trailing_stop", "reason": "P3 hard rule", "run_id": 3},
    ])
    (ev,) = site.tape_block(conn)
    assert ev["mechanism"] == "trailing_stop" and ev["note"] == "P3 hard rule"


def test_a_canceled_rule_is_two_events_armed_then_pulled():
    conn = FakeConn(orders=[
        {"agent_id": "maverick", "kind": "stop", "side": "sell", "symbol": "SHOP",
         "params": {"trigger_price": 107.0}, "reason": "P4 hard stop",
         "status": "canceled", "created_at": T(23), "closed_at": T(28)},
    ])
    events = site.tape_block(conn)
    assert [e["event"] for e in events] == ["pulled", "armed"]   # newest first
    assert all(e["trigger"] == 107.0 for e in events)


def test_an_open_rule_is_armed_only():
    conn = FakeConn(orders=[
        {"agent_id": "catalyst", "kind": "stop", "side": "sell", "symbol": "V",
         "params": {"trigger_price": 345.0}, "reason": "", "status": "open",
         "created_at": T(27), "closed_at": None},
    ])
    assert [e["event"] for e in site.tape_block(conn)] == ["armed"]


def test_a_refused_trade_is_on_the_tape_with_the_rule_that_refused_it():
    conn = FakeConn(rejected=[
        {"created_at": T(23), "agent_id": "catalyst", "payload":
            {"side": "buy", "symbol": "META", "notional_usd": 25000},
         "reason": "single-position cap 20% of equity breached"},
    ])
    (ev,) = site.tape_block(conn)
    assert ev["event"] == "blocked" and ev["symbol"] == "META"
    assert ev["notional"] == 25000
    assert ev["note"] == "single-position cap 20% of equity breached"


def test_the_tape_is_newest_first_and_capped():
    conn = FakeConn(fills=[
        {"ts": T(d), "agent_id": "a", "symbol": "X", "side": "buy", "qty": 1,
         "fill_price": 1.0, "kind": "market", "reason": None, "run_id": 0}
        for d in (20, 28, 24)
    ])
    events = site.tape_block(conn)
    assert [e["t"] for e in events] == sorted((e["t"] for e in events), reverse=True)
    assert len(site.tape_block(conn, limit=2)) == 2


# ---- the line the public artifact must not cross -------------------------

def test_system_block_publishes_freshness_and_nothing_else():
    class TickConn:
        def execute(self, sql, params=None):
            self._one = ({"t": T(28, 14, 3)} if "max(ts)" in sql
                         else {"n": 81})
            return self

        def fetchone(self):
            return self._one

    block = site.system_block(TickConn())
    assert block == {"last_update": "Jul 28 14:03", "symbols_tracked": 81}
    for banned in ("total_cost_usd", "runs", "ops", "triggers", "tokens_in"):
        assert banned not in block


# ---- what the public file may say about a face ---------------------------

def test_chosen_never_reaches_the_public_avatar():
    """`chosen` records whether the principal touched the seat's picker. It is
    provenance for the operator, not something the floor announces about a
    trader — and arena.json is served publicly."""
    cfg_avatar = {"base": "owl", "color": 3, "costume": "professor",
                  "acc": "rounds", "chosen": False}
    assert site.public_avatar(cfg_avatar) == {
        "base": "owl", "color": 3, "costume": "professor", "acc": "rounds"}
    assert "chosen" not in site.public_avatar(cfg_avatar)


def test_public_avatar_drops_any_key_it_was_not_asked_for():
    """The projection is a whitelist, so a future config key cannot leak into a
    public file by being added upstream."""
    out = site.public_avatar({"base": "fox", "color": 0, "costume": "suit",
                              "acc": "none", "chosen": True,
                              "owner_email": "someone@example.com", "notes": "x"})
    assert set(out) == set(site.AVATAR_PUBLIC_KEYS)


def test_public_avatar_survives_a_missing_or_malformed_avatar():
    for garbage in (None, "owl", 7, []):
        assert site.public_avatar(garbage) == {}


# ---- the sandbox stays off the floor --------------------------------------
# A test seating (engine/sandbox.py) runs like any other trader, so every
# projection the floor renders has to leave it out by name.

def test_a_sandbox_trader_is_kept_off_the_tape():
    conn = FakeConn(fills=[
        {"ts": T(29), "agent_id": "probe", "symbol": "AMD", "side": "buy", "qty": 1,
         "fill_price": 100.0, "kind": "market", "reason": None, "run_id": 1},
        {"ts": T(29), "agent_id": "tempo", "symbol": "AMD", "side": "buy", "qty": 1,
         "fill_price": 101.0, "kind": "market", "reason": None, "run_id": 2},
    ])
    assert [e["agent"] for e in site.tape_block(conn, skip={"probe"})] == ["tempo"]


def test_a_sandbox_trader_does_not_move_the_floor_index():
    """The index is the mean per-agent return each interval — one test trader
    doubling its money would otherwise show up as the floor's performance."""
    marks = [
        {"ts": T(28), "agent_id": "tempo", "equity": 100000.0},
        {"ts": T(28), "agent_id": "probe", "equity": 100000.0},
        {"ts": T(29), "agent_id": "tempo", "equity": 110000.0},
        {"ts": T(29), "agent_id": "probe", "equity": 200000.0},
    ]

    class Marks:
        def execute(self, sql, params=None):
            return self

        def fetchall(self):
            return marks

    curve = site.arena_curve(Marks(), skip={"probe"})
    assert [round(p["v"], 2) for p in curve] == [100.0, 110.0]
    # unfiltered, for contrast: the mean of +10% and +100% drags the floor to +55%
    assert round(site.arena_curve(Marks())[-1]["v"], 2) == 155.0
