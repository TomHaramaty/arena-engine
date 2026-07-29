"""Deterministic arena engine: fills, marks, standing orders, triggers.

Pure computation lives in module-level functions (unit-testable without a DB);
DB mutation happens in the apply_*/mark_* functions, each transactional.
"""
import json
from datetime import datetime, timezone

COST = 0.0015          # 0.15% slippage+fees, applied against the agent
DRAWDOWN_TRIGGER = 0.07  # wake the brain when equity drops >7% from peak
# Consecutive sessions without an order before inaction becomes reviewable.
# The arena's mandate is that inaction requires as much justification as action,
# but nothing enforced it: an agent whose entry conditions never fire produces
# no fills, no closed positions and no drawdown, so it generated no reflection
# event of any kind and could sit in cash indefinitely, unexamined. Three
# sessions is a day and a half — long enough to be a stance, short enough that
# the agent still remembers arguing for it.
DORMANT_SESSIONS = 3


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


def idle_streak(traded_flags):
    """traded_flags: did each session place an order, newest session first.
    → how many sessions in a row, ending at the most recent one, placed none."""
    n = 0
    for traded in traded_flags:
        if traded:
            break
        n += 1
    return n


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
                """insert into ticks (symbol, ts, price, prev_close, high, low, day_open)
                   values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
                (sym, q["ts"], q["price"], q["prev_close"],
                 q.get("high"), q.get("low"), q.get("open")),
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


def order_trigger_price(kind, side, params):
    """The price level this order fires at, as its params stand right now.
    For a trailing stop that is the level implied by the CURRENT high water —
    callers deciding whether a past window touched it must ask before raising
    the water with anything from that same window."""
    if kind == "trailing_stop":
        return float(params["high_water"]) * (1 - float(params["trail_pct"]))
    if kind == "stop":
        return float(params["trigger_price"])
    return float(params["limit_price"])


def fires_downward(kind, side):
    """stop/sell, trailing_stop and limit/buy fire when the market falls to
    them; stop/buy and limit/sell fire when it rises."""
    if kind == "trailing_stop":
        return True
    if kind == "stop":
        return side == "sell"
    return side == "buy"


def range_fire(kind, side, params, sess):
    """Was this order's trigger touched inside the unobserved gap since the
    last look — and if so, at what price does it honestly fill?

    The tick samples the market roughly hourly. Measured on the record
    (2026-07-29): 37% of a session's price range fell between samples, and
    the stops that fired had executed an average 1.19% past their own trigger
    against a modelled cost of 0.15%. The data source's /quote already
    reports the session high and low, so a touch is observable even when no
    sample landed on it.

    No hindsight: the session extremes are cumulative, so an order must never
    fire on an extreme printed before the engine first saw the order. params
    carries the baseline — seen_session / seen_high / seen_low, stamped when
    the order first meets each session:

      · first observation ever → stamp the baseline, no range fire (the
        current-price check still applies);
      · same session, later tick → fire only on a NEW extreme, beyond the
        baseline, that reaches the trigger — and fill AT the trigger, since
        the touch happened between looks, not at the sampled last price;
      · a fresh session for an order that lived through the close → the whole
        session is fair game, and if the market gapped straight through the
        trigger at the open, fill at the open. That is what really happens,
        and it is the conservative direction (ruled 2026-07-29).

    sess: {date, high, low, open}. Returns (fill_basis_or_None, new_baseline)
    — new_baseline must be persisted onto the order either way, so the next
    look knows what this one saw.
    """
    baseline = {
        "seen_session": sess["date"],
        "seen_high": sess["high"],
        "seen_low": sess["low"],
    }
    seen = params.get("seen_session")
    trig = order_trigger_price(kind, side, params)
    down = fires_downward(kind, side)

    if seen is None:
        # Everything printed so far predates the engine knowing this order.
        return None, baseline

    if seen == sess["date"]:
        if down:
            if sess["low"] <= trig and sess["low"] < float(params["seen_low"]):
                return trig, baseline
        else:
            if sess["high"] >= trig and sess["high"] > float(params["seen_high"]):
                return trig, baseline
        return None, baseline

    # The order lived through into a new session.
    if down and sess["low"] <= trig:
        return (sess["open"] if sess["open"] <= trig else trig), baseline
    if not down and sess["high"] >= trig:
        return (sess["open"] if sess["open"] >= trig else trig), baseline
    return None, baseline


def session_of(quote):
    """The session view of a quote, or None when the range must not be used:
    crypto reports a rolling 24h window (session_range False), and a payload
    missing any of high/low/open cannot support a touch decision."""
    if not quote.get("session_range", False):
        return None
    if not (quote.get("high") and quote.get("low") and quote.get("open")):
        return None
    return {
        "date": quote["ts"].date().isoformat(),
        "high": float(quote["high"]),
        "low": float(quote["low"]),
        "open": float(quote["open"]),
    }


def evaluate_standing_orders(conn, quotes, now=None):
    """Walk open stop/trailing/limit orders against fresh quotes; execute what
    triggers. Each execution also files a triggers_fired row so the dispatcher
    can wake the owning brain.

    An order fires two ways. The range check asks whether the session's
    high/low touched the trigger since the last look, and fills at the trigger
    (or the open, on a gap) — see range_fire for the no-hindsight baseline.
    The last-price check is the fallback for whatever the range cannot cover:
    crypto's rolling window, a payload without extremes, and an order meeting
    the engine for the first time already past its trigger."""
    now = now or datetime.now(timezone.utc)
    filled = []       # (order, fill basis, qty) — the caller's view
    fired_meta = []   # (order, basis, qty, via, trigger) — for the audit row
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
        q = quotes[o["symbol"]]
        price = q["price"]
        params = o["params"] or {}
        sess = session_of(q)
        updates = {}

        # The touch decision must use the trigger as it stood BEFORE this
        # window — a high printed in the same unobserved gap as the low must
        # not raise a trailing stop onto a level that may not have existed
        # when the low printed. So: range check first, water raises after.
        trig = order_trigger_price(o["kind"], o["side"], params)
        basis = via = None
        if sess:
            basis, baseline = range_fire(o["kind"], o["side"], params, sess)
            if basis is not None:
                via = "gap-open" if basis != trig else "range"
            for k, v in baseline.items():
                if params.get(k) != v:
                    updates[k] = v

        if o["kind"] == "trailing_stop":
            hw = max(float(params["high_water"]), price)
            # A session high raises the water only when it is live knowledge:
            # printed in a session this order was already being watched in
            # (not before the engine first saw it — that would be hindsight).
            prev_seen = params.get("seen_session")
            if sess and prev_seen is not None and (
                prev_seen != sess["date"]
                or sess["high"] > float(params.get("seen_high") or 0)
            ):
                hw = max(hw, sess["high"])
            if hw != float(params["high_water"]):
                updates["high_water"] = hw
                params = {**params, "high_water": hw}

        if updates:
            conn.execute(
                "update orders set params = params || %s::jsonb where id=%s",
                (json.dumps(updates), o["id"]),
            )
            params = {**params, **updates}

        if basis is None and triggered(o["kind"], o["side"], params, price):
            basis, via = price, "last"
        if basis is None:
            continue
        qty = (
            _execute_sell(conn, o, basis, now)
            if o["side"] == "sell"
            else _execute_buy(conn, o, basis, now, configs.get(o["agent_id"], {}))
        )
        if qty:
            filled.append((o, basis, qty))
            fired_meta.append((o, basis, qty, via, trig))
    for o, basis, qty, via, trig in fired_meta:
        conn.execute(
            """insert into triggers_fired (agent_id, kind, details, ts)
               values (%s, 'stop_filled', %s, %s)""",
            (
                o["agent_id"],
                json.dumps(
                    {"order_id": o["id"], "symbol": o["symbol"], "kind": o["kind"],
                     "side": o["side"], "qty": qty, "price": basis,
                     "via": via, "trigger_price": trig}
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


def flag_dormancy(conn, agent_id, threshold=DORMANT_SESSIONS):
    """File a 'dormant' trigger once an agent has gone `threshold` consecutive
    sessions without placing an order, so that inaction becomes reviewable the
    same way a closed position is.

    Filed handled=true: this is not a request to wake the brain (the agent has
    just deliberated — waking it to deliberate again would only produce the same
    argument), it is a request for the *reflection* to confront the inaction.
    Fires at most once per reflection period, so a genuinely patient agent is
    asked to justify itself once rather than at every session.

    Returns the streak length when a trigger is filed, else None.
    """
    rows = conn.execute(
        """select exists (
                 select 1 from operations o
                 where o.run_id = r.id and o.type = 'place_order'
                   and o.verdict = 'accepted') as traded
           from runs r
           where r.agent_id = %s and r.status = 'completed'
             and r.trigger not like 'reflection%%'
           order by r.started desc
           limit 50""",
        (agent_id,),
    ).fetchall()
    streak = idle_streak([r["traded"] for r in rows])
    if streak < threshold:
        return None

    since = conn.execute(
        """select max(started) t from runs
           where agent_id=%s and trigger like 'reflection%%' and status='completed'""",
        (agent_id,),
    ).fetchone()["t"]
    already = conn.execute(
        """select 1 from triggers_fired
           where agent_id=%s and kind='dormant' and (%s::timestamptz is null or ts > %s)
           limit 1""",
        (agent_id, since, since),
    ).fetchone()
    if already:
        return None

    conn.execute(
        """insert into triggers_fired (agent_id, kind, details, handled)
           values (%s,'dormant',%s,true)""",
        (agent_id, json.dumps({"sessions_without_orders": streak})),
    )
    conn.commit()
    return streak


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
