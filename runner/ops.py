"""Parse, validate (constitution-as-code), and apply brain operations."""
import json
import re
from datetime import datetime, timezone

from engine import core

CRYPTO = {"BTC-USD", "ETH-USD"}


class OpsParseError(Exception):
    pass


def parse(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        raise OpsParseError("no fenced json operations block in output")
    data = json.loads(blocks[-1])
    ops = data.get("operations")
    if not isinstance(ops, list) or not ops:
        raise OpsParseError("operations key missing or empty")
    n_journal = sum(1 for o in ops if o.get("type") == "journal_entry")
    if n_journal != 1:
        raise OpsParseError(f"expected exactly 1 journal_entry, got {n_journal}")
    return ops


def _latest_price(conn, symbol):
    r = conn.execute(
        "select price from ticks where symbol=%s order by ts desc limit 1", (symbol,)
    ).fetchone()
    return float(r["price"]) if r else None


def _position_qty(conn, agent_id, symbol):
    r = conn.execute(
        "select qty from positions where agent_id=%s and symbol=%s",
        (agent_id, symbol),
    ).fetchone()
    return float(r["qty"]) if r else 0.0


def _watchlisted(conn, symbol):
    return bool(
        conn.execute(
            "select 1 from watchlist where symbol=%s and status='active'", (symbol,)
        ).fetchone()
    )


def _crypto_value(conn, agent_id):
    total = 0.0
    for sym in CRYPTO:
        q = _position_qty(conn, agent_id, sym)
        if q:
            total += q * (_latest_price(conn, sym) or 0)
    return total


def validate_and_apply(conn, agent, run_id, ops, dry=False):
    """Returns list of (op, verdict, reason). Applies accepted mutating ops
    unless dry. agent: row with id, config. Journal handling is the caller's."""
    agent_id, cfg = agent["id"], agent["config"] or {}
    now = datetime.now(timezone.utc)
    results = []

    st = conn.execute(
        "select cash from agent_state where agent_id=%s", (agent_id,)
    ).fetchone()
    cash = float(st["cash"])
    # equity for cap math
    pos = conn.execute(
        "select symbol, qty from positions where agent_id=%s", (agent_id,)
    ).fetchall()
    equity = cash + sum(
        float(p["qty"]) * (_latest_price(conn, p["symbol"]) or 0) for p in pos
    )

    def record(op, verdict, reason=None):
        results.append((op, verdict, reason))
        if not dry:
            conn.execute(
                """insert into operations (run_id, seq, type, payload, verdict, reason)
                   values (%s,%s,%s,%s,%s,%s)""",
                (run_id, len(results), op.get("type", "?"), json.dumps(op), verdict, reason),
            )

    for op in ops:
        t = op.get("type")
        try:
            if t == "journal_entry":
                record(op, "accepted")

            elif t == "place_order":
                sym, side = op.get("symbol"), op.get("side")
                if side not in ("buy", "sell"):
                    record(op, "rejected", "side must be buy or sell"); continue
                if not _watchlisted(conn, sym):
                    record(op, "rejected", f"{sym} not on watchlist — file watchlist_request first"); continue
                price = _latest_price(conn, sym)
                if not price:
                    record(op, "rejected", f"no engine price for {sym}"); continue

                if side == "buy":
                    notional = float(op.get("notional_usd") or 0)
                    if notional <= 0:
                        record(op, "rejected", "buy needs positive notional_usd"); continue
                    if not (op.get("thesis") and op.get("invalidation") and op.get("review_by")):
                        record(op, "rejected", "buy needs thesis + invalidation + review_by"); continue
                    # The engine executes the constitutional maximum of the proposed
                    # intent: oversized buys are CLIPPED to cap/cash (recorded), not voided.
                    held_val = _position_qty(conn, agent_id, sym) * price
                    allowed, clip_reasons = notional, []
                    if sym in CRYPTO:
                        cap = cfg.get("crypto_core_cap_pct")
                        if cap is not None:
                            capacity = cap * equity - _crypto_value(conn, agent_id)
                            if allowed > capacity:
                                allowed = capacity; clip_reasons.append(f"crypto core cap {cap:.0%}")
                    else:
                        cap = cfg.get("max_single_equity_pct", cfg.get("max_single_pct"))
                        if cap is not None:
                            capacity = cap * equity - held_val
                            if allowed > capacity:
                                allowed = capacity; clip_reasons.append(f"single-position cap {cap:.0%}")
                    if allowed > cash:
                        allowed = cash; clip_reasons.append("available cash")
                    if allowed < min(500.0, notional):
                        record(op, "rejected",
                               f"no meaningful capacity: ${allowed:,.0f} left under " + ", ".join(clip_reasons)); continue
                    clip_note = (f" (clipped from ${notional:,.0f} to ${allowed:,.0f} by "
                                 + ", ".join(clip_reasons) + ")") if clip_reasons else ""
                    notional = allowed
                    fp = core.buy_fill_price(price)
                    qty = notional / fp
                    if not dry:
                        row = conn.execute(
                            """insert into orders (agent_id, kind, side, symbol, qty, params, status, run_id, created_at, closed_at)
                               values (%s,'market','buy',%s,%s,%s,'filled',%s,%s,%s) returning id""",
                            (agent_id, sym, qty, json.dumps({"notional_usd": notional}), run_id, now, now),
                        ).fetchone()
                        conn.execute(
                            """insert into fills (order_id, agent_id, symbol, side, qty, price, fill_price, ts)
                               values (%s,%s,%s,'buy',%s,%s,%s,%s)""",
                            (row["id"], agent_id, sym, qty, price, fp, now),
                        )
                        conn.execute(
                            """insert into positions (agent_id, symbol, qty, avg_fill, opened_at, thesis, invalidation, review_by)
                               values (%s,%s,%s,%s,%s,%s,%s,%s)
                               on conflict (agent_id, symbol) do update
                               set avg_fill=(positions.qty*positions.avg_fill + excluded.qty*excluded.avg_fill)
                                            /(positions.qty+excluded.qty),
                                   qty=positions.qty+excluded.qty,
                                   thesis=excluded.thesis, invalidation=excluded.invalidation,
                                   review_by=excluded.review_by""",
                            (agent_id, sym, qty, fp, now.date(), op["thesis"], op["invalidation"], op["review_by"]),
                        )
                        conn.execute(
                            "update agent_state set cash=cash-%s where agent_id=%s",
                            (notional, agent_id),
                        )
                    cash -= notional
                    record(op, "accepted", f"filled {qty:.4f} {sym} @ {fp:.2f}{clip_note}")

                else:  # sell
                    held = _position_qty(conn, agent_id, sym)
                    if held <= 0:
                        record(op, "rejected", f"no {sym} position (long-only, no shorts)"); continue
                    qty = held if op.get("qty") in ("all", None) else float(op["qty"])
                    if qty > held + 1e-9:
                        record(op, "rejected", f"sell qty {qty} exceeds position {held}"); continue
                    fp = core.sell_fill_price(price)
                    proceeds = qty * fp
                    if not dry:
                        row = conn.execute(
                            """insert into orders (agent_id, kind, side, symbol, qty, status, run_id, created_at, closed_at)
                               values (%s,'market','sell',%s,%s,'filled',%s,%s,%s) returning id""",
                            (agent_id, sym, qty, run_id, now, now),
                        ).fetchone()
                        conn.execute(
                            """insert into fills (order_id, agent_id, symbol, side, qty, price, fill_price, ts)
                               values (%s,%s,%s,'sell',%s,%s,%s,%s)""",
                            (row["id"], agent_id, sym, qty, price, fp, now),
                        )
                        if qty >= held - 1e-9:
                            conn.execute(
                                "delete from positions where agent_id=%s and symbol=%s",
                                (agent_id, sym),
                            )
                            # reflection-due marker (handled=true: not a wake request —
                            # the agent just acted; this schedules its post-mortem)
                            conn.execute(
                                """insert into triggers_fired (agent_id, kind, details, handled)
                                   values (%s,'position_closed',%s,true)""",
                                (agent_id, json.dumps({"symbol": sym, "qty": qty, "fill_price": fp})),
                            )
                        else:
                            conn.execute(
                                "update positions set qty=qty-%s where agent_id=%s and symbol=%s",
                                (qty, agent_id, sym),
                            )
                        conn.execute(
                            "update agent_state set cash=cash+%s where agent_id=%s",
                            (proceeds, agent_id),
                        )
                    cash += proceeds
                    record(op, "accepted", f"sold {qty:.4f} {sym} @ {fp:.2f}")

            elif t == "register_standing_order":
                sym, kind = op.get("symbol"), op.get("kind")
                if kind not in ("stop", "trailing_stop", "limit"):
                    record(op, "rejected", "kind must be stop|trailing_stop|limit"); continue
                if not _watchlisted(conn, sym):
                    record(op, "rejected", f"{sym} not on watchlist"); continue
                price = _latest_price(conn, sym)
                params = {}
                side = op.get("side") or ("sell" if kind != "limit" else "buy")
                if kind == "stop":
                    if not op.get("trigger_price"):
                        record(op, "rejected", "stop needs trigger_price"); continue
                    params["trigger_price"] = float(op["trigger_price"])
                elif kind == "trailing_stop":
                    if not op.get("trail_pct"):
                        record(op, "rejected", "trailing_stop needs trail_pct"); continue
                    params = {"trail_pct": float(op["trail_pct"]), "high_water": price}
                elif kind == "limit":
                    if not op.get("limit_price"):
                        record(op, "rejected", "limit needs limit_price"); continue
                    params["limit_price"] = float(op["limit_price"])
                    if side == "buy" and not op.get("qty"):
                        record(op, "rejected", "limit buy needs qty"); continue
                if side == "sell" and _position_qty(conn, agent_id, sym) <= 0:
                    record(op, "rejected", "no position to protect (long-only)"); continue
                if not dry:
                    conn.execute(
                        """insert into orders (agent_id, kind, side, symbol, qty, params, reason, run_id)
                           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (agent_id, kind, side, sym,
                         None if op.get("qty") in ("all", None) else float(op["qty"]),
                         json.dumps(params), op.get("note", ""), run_id),
                    )
                record(op, "accepted")

            elif t == "cancel_order":
                oid = op.get("order_id")
                r = conn.execute(
                    "select id from orders where id=%s and agent_id=%s and status='open'",
                    (oid, agent_id),
                ).fetchone()
                if not r:
                    record(op, "rejected", f"no open order {oid} for you"); continue
                if not dry:
                    conn.execute(
                        "update orders set status='canceled', reason=%s, closed_at=%s where id=%s",
                        (op.get("note", "canceled by agent"), now, oid),
                    )
                record(op, "accepted")

            elif t == "hypothesis_op":
                record(op, "accepted", "recorded (prose files updated at reflection)")

            elif t == "watchlist_request":
                sym = (op.get("symbol") or "").upper()
                if not sym or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", sym):
                    record(op, "rejected", "invalid symbol"); continue
                if _watchlisted(conn, sym):
                    record(op, "accepted", "already on watchlist")
                else:
                    if not dry:
                        conn.execute(
                            """insert into watchlist (symbol, source_symbol, requested_by, status)
                               values (%s,%s,%s,'active') on conflict do nothing""",
                            (sym, sym, agent_id),
                        )
                        conn.execute(
                            """insert into triggers_fired (agent_id, kind, details)
                               values (%s,'watchlist_granted',%s)""",
                            (agent_id, json.dumps({"symbol": sym})),
                        )
                    record(op, "accepted", "granted — quotes start next tick")

            else:
                record(op, "rejected", f"unknown op type {t}")
        except Exception as e:  # one bad op never poisons the batch
            record(op, "rejected", f"error: {e}")

    if not dry:
        conn.commit()
    return results
