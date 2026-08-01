"""What the trading cost, as the agent is told it.

The number these tests defend is one an agent reads and reasons from, so the
contract is not "a plausible figure" but "the figure the book actually
charged". Every assertion here recomputes the toll independently from
core.COST and the fills, and refuses to take the reporting function's word for
it — the failure mode worth fearing is a cost line that drifts from the ledger
and quietly tells 24 traders something untrue about themselves.

WHAT THESE TESTS DO NOT COVER, and how the gap was closed. FakeConn does not
execute SQL, it recognises statements and answers them from its own book, so
the aggregate in context.trading_cost is *reimplemented* by the fake rather
than run. These tests therefore defend the reporting layer (the sentence, the
label, the silence when nothing was traded, the division that could blow up)
and the end-to-end path from an accepted operation to a fill that gets
counted. They cannot defend the query.

The query was verified against the live record instead, on 2026-07-31: for
maverick, 15 fills, the SQL and an independent pass in Python over the raw
rows agreed to 1e-6 ($361,902.588988 notional, $542.853883 in frictions), and
frictions/notional came out at exactly 0.001500 against core.COST of 0.0015.
Re-run that check rather than trusting this file if the query changes.
"""
import json

import pytest

from engine import core
from runner import context, ops
from tests.fakedb import FakeConn, agent

RUN = 7


def block(*operations):
    return ("reasoning\n\n```json\n"
            + json.dumps({"operations": list(operations)}) + "\n```\n")


def journal():
    return {"type": "journal_entry", "title": "a day", "body_markdown": "body"}


def buy(symbol="AMD", notional=10_000):
    return {"type": "place_order", "side": "buy", "symbol": symbol,
            "notional_usd": notional, "thesis": "it goes up",
            "invalidation": "it does not", "review_by": "2026-08-15"}


def sell(symbol="AMD", qty="all"):
    return {"type": "place_order", "side": "sell", "symbol": symbol, "qty": qty}


def conn(**kw):
    kw.setdefault("ticks", {"AMD": 200.0, "NVDA": 100.0})
    kw.setdefault("watchlist", {"AMD": "equity", "NVDA": "equity"})
    return FakeConn(**kw)


def apply(c, *operations):
    return ops.validate_and_apply(c, agent(c.agent_id), RUN,
                                  ops.parse(block(journal(), *operations)))


# ---------- the arithmetic ----------

def test_a_trader_that_has_never_traded_is_told_nothing():
    """A row of zeroes reads as an accusation. Silence is the honest shape."""
    c = conn()
    assert context.trading_cost(c, c.agent_id)["n"] == 0
    assert context.cost_line(c, c.agent_id, 100_000.0) is None


def test_the_friction_reported_is_the_friction_charged():
    c = conn()
    apply(c, buy("AMD", 10_000))

    cost = context.trading_cost(c, c.agent_id)
    fill = c.fills[0]

    # Independent of the function under test: the engine fills a buy at
    # price*(1+COST) and takes the whole notional out of cash, so the toll is
    # exactly the difference between what was paid and what was bought.
    paid = fill["qty"] * fill["fill_price"]
    market_value = fill["qty"] * fill["price"]
    assert cost["n"] == 1
    assert cost["notional"] == pytest.approx(market_value)
    assert cost["frictions"] == pytest.approx(paid - market_value)
    assert cost["frictions"] == pytest.approx(market_value * core.COST)


def test_both_sides_of_a_round_trip_are_charged():
    c = conn()
    apply(c, buy("AMD", 20_000))
    apply(c, sell("AMD"))

    cost = context.trading_cost(c, c.agent_id)
    assert cost["n"] == 2
    assert [f["side"] for f in c.fills] == ["buy", "sell"]
    # A sell fills at price*(1-COST): the toll is taken out of the proceeds,
    # which is the same absolute distance from the engine price as a buy's.
    expected = sum(f["qty"] * f["price"] * core.COST for f in c.fills)
    assert cost["frictions"] == pytest.approx(expected)


def test_turnover_is_measured_against_equity_not_against_cash():
    """Two round trips on a $100k book is 4x turnover, whatever cash is left."""
    c = conn()
    apply(c, buy("AMD", 25_000))
    apply(c, sell("AMD"))
    apply(c, buy("NVDA", 25_000))
    apply(c, sell("NVDA"))

    line = context.cost_line(c, c.agent_id, 100_000.0)
    assert "4 fills" in line
    assert "1.0x your equity" in line


# ---------- the period filter ----------

def test_since_counts_only_the_fills_after_it():
    """The reflection asks about its own period. A cutoff that leaked earlier
    fills would make every reflection judge trades it had already judged."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = {"agent_id": "tempo", "symbol": "AMD", "side": "buy", "qty": 10.0,
           "price": 100.0, "fill_price": 100.15, "ts": now - timedelta(days=30)}
    new = {"agent_id": "tempo", "symbol": "AMD", "side": "buy", "qty": 5.0,
           "price": 100.0, "fill_price": 100.15, "ts": now - timedelta(hours=1)}
    c = conn(fills=[old, new])

    assert context.trading_cost(c, "tempo")["n"] == 2
    since = now - timedelta(days=7)
    recent = context.trading_cost(c, "tempo", since=since)
    assert recent["n"] == 1
    assert recent["notional"] == pytest.approx(500.0)


def test_another_agents_fills_are_never_counted():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    theirs = {"agent_id": "maverick", "symbol": "AMD", "side": "buy",
              "qty": 1000.0, "price": 100.0, "fill_price": 100.15, "ts": now}
    c = conn(fills=[theirs])
    assert context.trading_cost(c, "tempo")["n"] == 0


# ---------- the sentence an agent actually reads ----------

def test_the_line_states_the_toll_in_dollars_and_in_percent():
    c = conn()
    apply(c, buy("AMD", 40_000))
    line = context.cost_line(c, c.agent_id, 100_000.0)

    cost = context.trading_cost(c, c.agent_id)
    assert f"${cost['frictions']:,.0f} paid in frictions" in line
    assert f"{cost['frictions'] / 100_000 * 100:.2f}% of your equity" in line
    assert "since you launched" in line


def test_the_label_follows_the_period_being_asked_about():
    c = conn()
    apply(c, buy("AMD", 10_000))
    assert "this period" in context.cost_line(
        c, c.agent_id, 100_000.0, label="this period")


def test_a_book_with_no_equity_does_not_divide_by_zero():
    """An agent can only reach zero equity through fills, so the line is being
    built exactly when the arithmetic is most likely to blow up."""
    c = conn()
    apply(c, buy("AMD", 10_000))
    assert context.cost_line(c, c.agent_id, 0.0) is not None
