"""Parse, validate (constitution-as-code), and apply brain operations."""
import json
import re
from datetime import datetime, timezone

from engine import core, marketdata


class OpsParseError(Exception):
    pass


def parse(text):
    """The operations a session decided on, from the last fenced block that
    actually carries them.

    It used to read `blocks[-1]` and nothing else. That was safe against the
    managed agent, whose output shape was stable; against a model it is not —
    a session on 2026-07-31 emitted a second fence after its operations and
    the whole run died on a JSONDecodeError with the real operations sitting
    intact in the block above. Now every candidate is tried, newest first.

    This cannot invent anything: a block is only accepted if it parses AND
    carries a non-empty `operations` list. A truncated or malformed block is
    still a failed session, which is the honest outcome.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        raise OpsParseError("no fenced json operations block in output")
    ops, last_error = None, None
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError as e:
            last_error = e
            continue
        candidate = data.get("operations") if isinstance(data, dict) else None
        if isinstance(candidate, list) and candidate:
            ops = candidate
            break
    if ops is None:
        if last_error and len(blocks) == 1:
            raise OpsParseError(f"the operations block is not valid json: {last_error}")
        raise OpsParseError(
            f"none of the {len(blocks)} fenced json block(s) carried operations")
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


def validate_and_apply(conn, agent, run_id, ops, dry=False, resolver=None):
    """Returns list of (op, verdict, reason). Applies accepted mutating ops
    unless dry. agent: row with id, config. Journal handling is the caller's.
    resolver: symbol -> resolution dict (see marketdata.resolve); injectable so
    tests never touch the network."""
    resolver = resolver or marketdata.resolve
    agent_id, cfg = agent["id"], agent["config"] or {}
    now = datetime.now(timezone.utc)
    results = []

    # Cap math reads the book fresh per operation (core.buy_allowance), so a
    # batch that buys twice cannot spend the same capacity twice.

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
        # A DB error mid-op (deadlock, etc.) aborts the transaction; without a
        # savepoint the record() call below would itself raise
        # InFailedSqlTransaction instead of recording the rejection.
        conn.execute("SAVEPOINT op")
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
                    allowed, clip_reasons = core.buy_allowance(
                        conn, agent_id, cfg, sym, notional)
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
                    record(op, "accepted", f"sold {qty:.4f} {sym} @ {fp:.2f}")

            elif t == "register_standing_order":
                sym, kind = op.get("symbol"), op.get("kind")
                if kind not in ("stop", "trailing_stop", "limit"):
                    record(op, "rejected", "kind must be stop|trailing_stop|limit"); continue
                if not _watchlisted(conn, sym):
                    record(op, "rejected", f"{sym} not on watchlist"); continue
                price = _latest_price(conn, sym)
                params = {}
                # Default side by kind: a trailing stop only ever protects; a
                # plain stop or limit can open or close, so it must say which.
                side = op.get("side") or ("sell" if kind != "limit" else "buy")
                if side not in ("buy", "sell"):
                    record(op, "rejected", "side must be buy or sell"); continue
                if kind == "trailing_stop" and side != "sell":
                    record(op, "rejected", "trailing_stop is a sell-side protection"); continue
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
                if side == "buy":
                    # Buys size in notional like every other buy; qty is still
                    # accepted. Capacity is checked at trigger, not here.
                    notional = float(op.get("notional_usd") or 0)
                    if notional > 0:
                        params["notional_usd"] = notional
                    elif not op.get("qty"):
                        record(op, "rejected",
                               f"{kind} buy needs notional_usd (or qty)"); continue
                    if op.get("thesis"):
                        params["thesis"] = op["thesis"]
                elif _position_qty(conn, agent_id, sym) <= 0:
                    record(op, "rejected", "no position to sell (long-only, no shorts)"); continue
                # An order whose trigger is already through the market is a
                # market order with extra steps — accept it, but say so.
                immediate = price is not None and core.triggered(
                    kind, side, params, price)
                if not dry:
                    conn.execute(
                        """insert into orders (agent_id, kind, side, symbol, qty, params, reason, run_id)
                           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (agent_id, kind, side, sym,
                         None if op.get("qty") in ("all", None) else float(op["qty"]),
                         json.dumps(params), op.get("note", ""), run_id),
                    )
                record(op, "accepted",
                       f"already through the market at {price:,.2f} — fills at the next tick"
                       if immediate else None)

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

            elif t == "guidance_response":
                # The principal's note has standing and no authority: the agent
                # must answer it, and the answer itself is what enters the
                # record. The engine only checks that the answer is real and
                # addressed to a note that is actually waiting.
                cid = str(op.get("cid") or "").strip().upper()
                disp = str(op.get("disposition") or "").strip().lower()
                note = str(op.get("note") or "").strip()
                g = conn.execute(
                    """select id from guidance where agent_id=%s and cid=%s
                       and disposition is null""", (agent_id, cid)).fetchone()
                if not g:
                    record(op, "rejected",
                           f"no unanswered guidance {cid or '(none given)'} for you"); continue
                if disp not in ("adopted", "converted", "declined", "refused"):
                    record(op, "rejected",
                           "disposition must be adopted, converted, declined or refused"); continue
                if len(note) < 20:
                    record(op, "rejected",
                           "your principal gets an answer in your own words, not a label"); continue
                if disp == "converted" and not any(
                        o.get("type") == "hypothesis_op" and o.get("op") == "propose"
                        for o in ops):
                    record(op, "rejected",
                           "converted means you filed the test: propose the hypothesis "
                           "in this same operations block"); continue
                if not dry:
                    conn.execute(
                        """update guidance set disposition=%s, answer=%s,
                           answered_run=%s, answered_at=%s where id=%s""",
                        (disp, note, run_id, now, g["id"]))
                record(op, "accepted", f"{cid} answered: {disp}")

            elif t == "watchlist_request":
                sym = (op.get("symbol") or "").upper()
                if not sym or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", sym):
                    record(op, "rejected", "invalid symbol"); continue
                if _watchlisted(conn, sym):
                    record(op, "accepted", "already on watchlist"); continue
                # Resolve before granting: the symbol must actually quote, and
                # we must store the source symbol it quotes under (SOL-USD ->
                # BINANCE:SOLUSDT). A grant that never quotes is worse than a
                # rejection — it reads as permission and gives none.
                try:
                    r = resolver(sym)
                except marketdata.QuoteError as e:
                    # "We could not ask" is not "the answer is no". Say which.
                    record(op, "rejected",
                           f"could not reach the data source to resolve {sym} ({e}) — "
                           "this is not a verdict on the symbol; request it again")
                    continue
                if not r:
                    record(op, "rejected",
                           f"{sym} does not resolve to a quotable instrument — the arena "
                           "prices US-listed equities, ADRs and ETFs, and major crypto "
                           "pairs; not forex, foreign listings, indices or options")
                    continue
                if not dry:
                    conn.execute(
                        """insert into watchlist (symbol, source_symbol, asset_class,
                                                  description, requested_by, status)
                           values (%s,%s,%s,%s,%s,'active') on conflict do nothing""",
                        (sym, r["source_symbol"], r["asset_class"],
                         r["description"], agent_id),
                    )
                    # Seed the resolving quote as a tick so the symbol is
                    # tradable in THIS run: a grant an agent cannot act on for
                    # another six hours is a grant no brain spends an op on.
                    core.insert_ticks(conn, {sym: r["quote"]})
                    conn.execute(
                        """insert into triggers_fired (agent_id, kind, details, handled)
                           values (%s,'watchlist_granted',%s,true)""",
                        (agent_id, json.dumps(
                            {"symbol": sym, "source_symbol": r["source_symbol"],
                             "asset_class": r["asset_class"]})),
                    )
                record(op, "accepted",
                       f"granted — {r['description']} ({r['asset_class']}) @ "
                       f"{r['quote']['price']:,.2f}; tradable now, this run")

            else:
                record(op, "rejected", f"unknown op type {t}")
        except Exception as e:  # one bad op never poisons the batch
            conn.execute("ROLLBACK TO SAVEPOINT op")
            record(op, "rejected", f"error: {e}")
        finally:
            # Observed in production (psycopg.errors.InvalidSavepointSpecification:
            # savepoint "op" does not exist) with no exception at all in the try/except
            # above — some ops leave the savepoint already gone by the time we get
            # here. Releasing it is just cleanup; if there's nothing left to release,
            # there's nothing to do, and the alternative is losing every remaining op
            # and agent in the run to an exception from a no-op.
            try:
                conn.execute("RELEASE SAVEPOINT op")
            except Exception:
                pass

    if not dry:
        conn.commit()
    return results
