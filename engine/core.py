"""Deterministic arena engine: fills, marks, standing orders, triggers.

Pure computation lives in module-level functions (unit-testable without a DB);
DB mutation happens in the apply_*/mark_* functions, each transactional.
"""
import json
from datetime import datetime, timezone

COST = 0.0015          # 0.15% slippage+fees, applied against the agent
DRAWDOWN_TRIGGER = 0.07  # wake the brain when equity drops >7% from peak


# ---------- pure functions ----------

def buy_fill_price(price):
    return price * (1 + COST)


def sell_fill_price(price):
    return price * (1 - COST)


def equity_of(cash, positions, prices):
    """positions: [{symbol, qty}] · prices: {symbol: price}. Positions with no
    fresh price are valued at their last known avg_fill by the caller before
    calling — here every position must have a price."""
    return cash + sum(p["qty"] * prices[p["symbol"]] for p in positions)


def bench_index(bench, prices):
    """bench: {symbols, weights, launch_prices} → weighted index, 100 = launch.
    Returns None if any component price is missing."""
    total = 0.0
    for sym, w, launch in zip(
        bench["symbols"], bench["weights"], bench["launch_prices"]
    ):
        if sym not in prices or not launch:
            return None
        total += w * (prices[sym] / launch)
    return round(total * 100, 4)


def trailing_state(params, price):
    """Given trailing_stop params {trail_pct, high_water} and current price,
    return (new_high_water, triggered)."""
    hw = max(float(params["high_water"]), price)
    triggered = price <= hw * (1 - float(params["trail_pct"]))
    return hw, triggered


def stop_triggered(params, price):
    return price <= float(params["trigger_price"])


def limit_buy_triggered(params, price):
    return price <= float(params["limit_price"])


# ---------- DB operations ----------

def insert_ticks(conn, quotes):
    with conn.cursor() as cur:
        for sym, q in quotes.items():
            cur.execute(
                """insert into ticks (symbol, ts, price, prev_close)
                   values (%s, %s, %s, %s) on conflict do nothing""",
                (sym, q["ts"], q["price"], q["prev_close"]),
            )
    conn.commit()


def _execute_sell(conn, order, price, now):
    """Sell entire order qty (or full position if order.qty is null)."""
    fp = sell_fill_price(price)
    with conn.cursor() as cur:
        cur.execute(
            "select qty from positions where agent_id=%s and symbol=%s",
            (order["agent_id"], order["symbol"]),
        )
        row = cur.fetchone()
        if not row or row["qty"] <= 0:
            cur.execute(
                "update orders set status='canceled', reason='no position', closed_at=%s where id=%s",
                (now, order["id"]),
            )
            return None
        qty = min(float(row["qty"]), float(order["qty"] or row["qty"]))
        proceeds = qty * fp
        remaining = float(row["qty"]) - qty
        if remaining > 1e-9:
            cur.execute(
                "update positions set qty=%s where agent_id=%s and symbol=%s",
                (remaining, order["agent_id"], order["symbol"]),
            )
        else:
            cur.execute(
                "delete from positions where agent_id=%s and symbol=%s",
                (order["agent_id"], order["symbol"]),
            )
        cur.execute(
            "update agent_state set cash = cash + %s where agent_id=%s",
            (proceeds, order["agent_id"]),
        )
        cur.execute(
            """insert into fills (order_id, agent_id, symbol, side, qty, price, fill_price, ts)
               values (%s,%s,%s,'sell',%s,%s,%s,%s)""",
            (order["id"], order["agent_id"], order["symbol"], qty, price, fp, now),
        )
        cur.execute(
            "update orders set status='filled', closed_at=%s where id=%s",
            (now, order["id"]),
        )
    return qty


def _execute_limit_buy(conn, order, price, now):
    fp = buy_fill_price(price)
    qty = float(order["qty"])
    cost = qty * fp
    with conn.cursor() as cur:
        cur.execute(
            "select cash from agent_state where agent_id=%s", (order["agent_id"],)
        )
        cash = float(cur.fetchone()["cash"])
        if cost > cash:
            cur.execute(
                "update orders set status='rejected', reason='insufficient cash at trigger', closed_at=%s where id=%s",
                (now, order["id"]),
            )
            return False
        cur.execute(
            "update agent_state set cash = cash - %s where agent_id=%s",
            (cost, order["agent_id"]),
        )
        cur.execute(
            """insert into positions (agent_id, symbol, qty, avg_fill, opened_at, thesis)
               values (%s,%s,%s,%s,%s,%s)
               on conflict (agent_id, symbol) do update
               set avg_fill = (positions.qty*positions.avg_fill + excluded.qty*excluded.avg_fill)
                              / (positions.qty + excluded.qty),
                   qty = positions.qty + excluded.qty""",
            (
                order["agent_id"], order["symbol"], qty, fp, now.date(),
                (order["params"] or {}).get("thesis", ""),
            ),
        )
        cur.execute(
            """insert into fills (order_id, agent_id, symbol, side, qty, price, fill_price, ts)
               values (%s,%s,%s,'buy',%s,%s,%s,%s)""",
            (order["id"], order["agent_id"], order["symbol"], qty, price, fp, now),
        )
        cur.execute(
            "update orders set status='filled', closed_at=%s where id=%s",
            (now, order["id"]),
        )
    return True


def evaluate_standing_orders(conn, quotes, now=None):
    """Walk open stop/trailing/limit orders against fresh quotes; execute what
    triggers. Each execution also files a triggers_fired row so the dispatcher
    can wake the owning brain."""
    now = now or datetime.now(timezone.utc)
    filled = []
    with conn.cursor() as cur:
        cur.execute(
            "select * from orders where status='open' and kind in ('stop','trailing_stop','limit')"
        )
        open_orders = cur.fetchall()
    for o in open_orders:
        if o["symbol"] not in quotes:
            continue
        price = quotes[o["symbol"]]["price"]
        params = o["params"] or {}
        if o["kind"] == "trailing_stop":
            hw, trig = trailing_state(params, price)
            if hw != float(params["high_water"]):
                conn.execute(
                    "update orders set params = params || %s::jsonb where id=%s",
                    (json.dumps({"high_water": hw}), o["id"]),
                )
            if trig:
                qty = _execute_sell(conn, o, price, now)
                if qty:
                    filled.append((o, price, qty))
        elif o["kind"] == "stop" and o["side"] == "sell":
            if stop_triggered(params, price):
                qty = _execute_sell(conn, o, price, now)
                if qty:
                    filled.append((o, price, qty))
        elif o["kind"] == "limit" and o["side"] == "buy":
            if limit_buy_triggered(params, price):
                if _execute_limit_buy(conn, o, price, now):
                    filled.append((o, price, float(o["qty"])))
    for o, price, qty in filled:
        conn.execute(
            """insert into triggers_fired (agent_id, kind, details, ts)
               values (%s, 'stop_filled', %s, %s)""",
            (
                o["agent_id"],
                json.dumps(
                    {"order_id": o["id"], "symbol": o["symbol"], "kind": o["kind"],
                     "side": o["side"], "qty": qty, "price": price}
                ),
                now,
            ),
        )
    conn.commit()
    return filled


def bootstrap_launches(conn, quotes, now=None):
    """First bell for seated-but-unlaunched agents (agent_state.launched is
    null — how jobs/ingest.py seats them): once every benchmark symbol has a
    fresh quote, stamp launched and record bench launch_prices. Until then
    mark_all skips them, so the record starts exactly at the bell."""
    now = now or datetime.now(timezone.utc)
    prices = {s: q["price"] for s, q in quotes.items()}
    launched = []
    with conn.cursor() as cur:
        cur.execute("select agent_id, bench from agent_state where launched is null")
        rows = cur.fetchall()
    for r in rows:
        bench = r["bench"]
        syms = bench.get("symbols") or []
        if not syms or any(s not in prices for s in syms):
            continue
        if not bench.get("launch_prices"):
            bench["launch_prices"] = [prices[s] for s in syms]
        conn.execute(
            "update agent_state set launched=%s, bench=%s where agent_id=%s",
            (now.date(), json.dumps(bench), r["agent_id"]),
        )
        launched.append(r["agent_id"])
    conn.commit()
    return launched


def mark_all(conn, quotes, now=None):
    """Mark every active agent's portfolio; update peak; detect drawdown
    triggers. Skips an agent if any of its position symbols lacks a fresh
    quote (never mark with stale/partial data)."""
    now = now or datetime.now(timezone.utc)
    prices = {s: q["price"] for s, q in quotes.items()}
    marked, skipped = [], []
    with conn.cursor() as cur:
        cur.execute(
            """select a.id, s.cash, s.peak_equity, s.bench
               from agents a join agent_state s on s.agent_id=a.id
               where a.status='active' and s.launched is not null"""
        )
        states = cur.fetchall()
    for st in states:
        with conn.cursor() as cur:
            cur.execute(
                "select symbol, qty from positions where agent_id=%s", (st["id"],)
            )
            positions = cur.fetchall()
        missing = [p["symbol"] for p in positions if p["symbol"] not in prices]
        if missing:
            skipped.append((st["id"], missing))
            continue
        pos_value = sum(float(p["qty"]) * prices[p["symbol"]] for p in positions)
        equity = float(st["cash"]) + pos_value
        bidx = bench_index(st["bench"], prices)
        peak = max(float(st["peak_equity"]), equity)
        conn.execute(
            """insert into equity_marks (agent_id, ts, equity, cash, positions_value, bench_index)
               values (%s,%s,%s,%s,%s,%s) on conflict do nothing""",
            (st["id"], now, equity, st["cash"], pos_value, bidx),
        )
        conn.execute(
            "update agent_state set peak_equity=%s where agent_id=%s",
            (peak, st["id"]),
        )
        if equity < peak * (1 - DRAWDOWN_TRIGGER):
            with conn.cursor() as cur:
                cur.execute(
                    """select 1 from triggers_fired
                       where agent_id=%s and kind='drawdown' and not handled""",
                    (st["id"],),
                )
                if not cur.fetchone():
                    conn.execute(
                        """insert into triggers_fired (agent_id, kind, details, ts)
                           values (%s,'drawdown',%s,%s)""",
                        (
                            st["id"],
                            json.dumps({"equity": equity, "peak": peak}),
                            now,
                        ),
                    )
        marked.append((st["id"], round(equity, 2), bidx))
    conn.commit()
    return marked, skipped
