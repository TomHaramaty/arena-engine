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


def limit_sell_triggered(params, price):
    """Take-profit: sell once the market reaches the asking price."""
    return price >= float(params["limit_price"])


def stop_buy_triggered(params, price):
    """Breakout entry: buy once the market trades up through the trigger."""
    return price >= float(params["trigger_price"])


# ---------- constitution: what a buy is allowed to be ----------

# An asset class an agent's constitution does not name is capped at zero. New
# markets are opened to an agent by charter, never by the arena's defaults
# widening underneath it.
DEFAULT_CLASS_CAPS = {
    "equity": None,         # bounded by the single-position cap and cash, not by class
    "etf": None,
    "crypto": 0.0,
    "inverse_levered": 0.0,
}


def class_caps(cfg):
    """Per-class ceilings, as a fraction of equity, from an agent's config.
    `crypto_core_cap_pct` is the legacy spelling of the crypto sleeve cap and
    still binds; `class_caps` is the general form."""
    caps = dict(DEFAULT_CLASS_CAPS)
    if cfg.get("crypto_core_cap_pct") is not None:
        caps["crypto"] = float(cfg["crypto_core_cap_pct"])
    for k, v in (cfg.get("class_caps") or {}).items():
        caps[k] = float(v)
    return caps


def symbol_cap(cfg, asset_class):
    """The single-position ceiling that binds this symbol, or None.

    Two spellings, and the difference is chartered, not cosmetic:
    `max_single_equity_pct` was written as "max single *equity* position", which
    covers everything listed — plain shares, ETFs, and inverse or leveraged
    ETFs, which are listed funds bought outright like any other. Only crypto
    sits outside that wording (Wildcat's sleeve is governed by its class cap
    alone). The unqualified `max_single_pct` binds every position.
    """
    if asset_class == "crypto":
        cap = cfg.get("max_single_pct")
    else:
        cap = cfg.get("max_single_equity_pct", cfg.get("max_single_pct"))
    return None if cap is None else float(cap)


def buy_capacity(requested, *, equity, cash, cap_pct, held_value,
                 class_cap_pct, class_held_value, cap_label, class_label):
    """→ (allowed_notional, [reasons it was cut]). Pure.

    The engine executes the constitutional maximum of a proposed intent:
    oversized buys are clipped and the clipping recorded, never silently
    voided. Every ceiling is measured against current equity, so an agent
    cannot outrun a cap by making the buy in slices."""
    allowed, reasons = float(requested), []
    if class_cap_pct is not None:
        room = class_cap_pct * equity - class_held_value
        if allowed > room:
            allowed = room
            reasons.append(f"{class_label} cap {class_cap_pct:.0%}")
    if cap_pct is not None:
        room = cap_pct * equity - held_value
        if allowed > room:
            allowed = room
            reasons.append(f"{cap_label} cap {cap_pct:.0%}")
    if allowed > cash:
        allowed = cash
        reasons.append("available cash")
    return max(allowed, 0.0), reasons


def asset_classes(conn):
    """{symbol: asset_class} for everything the arena prices. Symbols absent
    from the watchlist (delisted, retired) read as plain equity."""
    return {
        r["symbol"]: r["asset_class"]
        for r in conn.execute("select symbol, asset_class from watchlist").fetchall()
    }


def buy_allowance(conn, agent_id, cfg, symbol, requested, prices=None):
    """Constitutional capacity for one buy, read from live book state.
    → (allowed_notional, [reasons])."""
    cfg = cfg or {}
    state = conn.execute(
        "select cash from agent_state where agent_id=%s", (agent_id,)
    ).fetchone()
    cash = float(state["cash"])
    positions = conn.execute(
        "select symbol, qty from positions where agent_id=%s", (agent_id,)
    ).fetchall()
    if prices is None:
        prices = {
            r["symbol"]: float(r["price"])
            for r in conn.execute(
                "select distinct on (symbol) symbol, price from ticks"
                " order by symbol, ts desc"
            ).fetchall()
        }
    classes = asset_classes(conn)
    cls = classes.get(symbol, "equity")

    equity, held_value, class_held_value = cash, 0.0, 0.0
    for p in positions:
        value = float(p["qty"]) * prices.get(p["symbol"], 0.0)
        equity += value
        if p["symbol"] == symbol:
            held_value += value
        if classes.get(p["symbol"], "equity") == cls:
            class_held_value += value

    caps = class_caps(cfg)
    return buy_capacity(
        requested,
        equity=equity,
        cash=cash,
        cap_pct=symbol_cap(cfg, cls),
        held_value=held_value,
        class_cap_pct=caps.get(cls, 0.0),
        class_held_value=class_held_value,
        cap_label="single-position",
        class_label=cls.replace("_", "/"),
    )


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


def _execute_buy(conn, order, price, now, cfg):
    """Fill a triggered buy (limit or stop). The order may name a qty or a
    notional; either way the fill is clipped to the agent's constitutional
    capacity **at trigger time**, not at registration time — a standing order
    registered when a cap had room must not fill through that cap hours later.
    """
    fp = buy_fill_price(price)
    params = order["params"] or {}
    requested = (
        float(order["qty"]) * fp
        if order["qty"] is not None
        else float(params.get("notional_usd") or 0)
    )
    allowed, reasons = buy_allowance(
        conn, order["agent_id"], cfg, order["symbol"], requested
    )
    if allowed < min(500.0, requested):
        with conn.cursor() as cur:
            cur.execute(
                "update orders set status='rejected', reason=%s, closed_at=%s where id=%s",
                (f"no capacity at trigger: ${allowed:,.0f} under "
                 + ", ".join(reasons), now, order["id"]),
            )
        return False
    cost = allowed
    qty = cost / fp
    with conn.cursor() as cur:
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
    return qty


def triggered(kind, side, params, price):
    """Does this open order fire at this price? Four shapes, by (kind, side):
    stop/sell and trailing_stop protect a position on the way down; limit/buy
    and stop/buy open one — the first on weakness, the second on strength."""
    if kind == "trailing_stop":
        return trailing_state(params, price)[1]
    if kind == "stop":
        return stop_triggered(params, price) if side == "sell" \
            else stop_buy_triggered(params, price)
    if kind == "limit":
        return limit_buy_triggered(params, price) if side == "buy" \
            else limit_sell_triggered(params, price)
    return False


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
        cur.execute("select id, config from agents")
        configs = {r["id"]: r["config"] or {} for r in cur.fetchall()}
    for o in open_orders:
        if o["symbol"] not in quotes:
            continue
        price = quotes[o["symbol"]]["price"]
        params = o["params"] or {}
        if o["kind"] == "trailing_stop":
            hw = max(float(params["high_water"]), price)
            if hw != float(params["high_water"]):
                conn.execute(
                    "update orders set params = params || %s::jsonb where id=%s",
                    (json.dumps({"high_water": hw}), o["id"]),
                )
                params = {**params, "high_water": hw}
        if not triggered(o["kind"], o["side"], params, price):
            continue
        qty = (
            _execute_sell(conn, o, price, now)
            if o["side"] == "sell"
            else _execute_buy(conn, o, price, now, configs.get(o["agent_id"], {}))
        )
        if qty:
            filled.append((o, price, qty))
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
