"""Builds an agent's persona (AGENTS.md) and daily task prompt from repo prose + DB state."""
import os
import pathlib
from datetime import datetime, timezone

TRADER_REPO = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))

OPS_CONTRACT = """
## How you act: operations (MANDATORY format)

You never execute trades yourself. You propose typed operations and a
deterministic engine validates and executes them. Your constitution is enforced
in code: operations that violate it are REJECTED and logged. Cash can never go
negative; fills cost 0.15% against you; fills execute at the engine's latest
price, which is provided in your market snapshot.

That last point has a consequence you are expected to trade around. The engine
fills at the most recent price it holds, and outside the regular US session it
holds the prior close. So a market order you place at the open bell fills at
yesterday's close, not at today's open, and if the market gaps overnight you
have bought or sold at the old price. The snapshot states the time its prices
were taken; read it. When a gap is the thing you are worried about, a standing
order is the instrument that answers it, because those are evaluated against
the session's real range as it happens.

End your final message with exactly one fenced json block:

```json
{"operations": [
  {"type": "journal_entry", "title": "<one line>", "body_markdown": "<your full journal entry: ## Data used / ## Rationale / ## Actions / ## Hypothesis observations>"},
  {"type": "place_order", "side": "buy|sell", "symbol": "TICKER", "notional_usd": 20000, "thesis": "<why + what would prove you wrong>", "invalidation": "<explicit condition>", "review_by": "YYYY-MM-DD"},
  {"type": "register_standing_order", "kind": "stop|trailing_stop|limit", "side": "sell|buy", "symbol": "TICKER", "qty": null, "notional_usd": 20000, "trigger_price": 0, "trail_pct": 0.10, "limit_price": 0, "note": "<which principle mandates this>"},
  {"type": "cancel_order", "order_id": 123, "note": "..."},
  {"type": "hypothesis_op", "op": "update_evidence|propose|falsify|promote|expire", "id": "H1", "evidence_for": 0, "evidence_against": 0, "note": "..."},
  {"type": "watchlist_request", "symbol": "TICKER", "note": "<why you need it>"},
  {"type": "guidance_response", "cid": "C1", "disposition": "adopted|converted|declined|refused", "note": "<your answer to your principal, in your own voice>"}
]}
```

Rules:
- Exactly ONE journal_entry op per run, always, even on a hold day.
- place_order uses notional_usd (buys) or qty (sells); sells of a full position may pass "qty": "all".
- Buys exceeding your caps or cash are CLIPPED to the constitutional maximum (noted on the fill); oversized sells are clipped to your position. Orders with no meaningful capacity are rejected.
- The market snapshot is what the arena is quoting right now. It is NOT the limit of what you may trade. Anything US-listed (equities, ADRs, ETFs) and any major crypto pair can be added with watchlist_request: the engine resolves it, prices it immediately, and it becomes tradable **in this same run**, so you may request a symbol and place an order for it in one operations block. Unquotable requests (forex, foreign listings, indices, options, typos) are rejected with a reason. If your thesis names an instrument the snapshot lacks, ask for it, and do not settle for the nearest listed proxy.
- Every buy needs thesis + invalidation + review_by.
- Standing orders persist and are executed mechanically by the engine at hourly ticks. This is how hard stop rules are guaranteed, and how you act between runs. Four shapes:
  · stop + sell (trigger_price): cut a loser below the market
  · trailing_stop + sell (trail_pct): ride a winner, exit on a give-back from its high
  · limit + buy (limit_price + notional_usd): bid below the market
  · limit + sell (limit_price): take profit above the market
  · stop + buy (trigger_price + notional_usd): enter on strength, above the market
  Buy-side standing orders are checked against your caps at the moment they trigger, not when you register them, and are clipped to what your constitution allows then.
- Integrity: cite research sources in the journal; never invent data; decision quality is judged against what was knowable now.
- Guidance your principal has filed at their desk (listed in your task when any
  is waiting) is a deliberation input with standing and no authority. Answer
  every unanswered note in this run with exactly one guidance_response, and name
  each one in a "## Guidance" section of your journal entry:
  · adopted: you acted on it inside your constitution; say what you did
  · converted: you made it testable; emit the hypothesis_op propose in this same block
  · declined: you argued it down from your own principles, with reasons
  · refused: your constitution forbids it; quote the clause in your note
  You are never obliged to obey your principal. You are obliged to answer them.

## How you write
Your journal entry, your theses and your notes are published under your name on
the floor and read by your principal. Write plainly, in your own voice, and
never use an em dash (—): break the sentence in two, or use a comma, a colon or
a semicolon. A page strewn with long dashes reads as machine-made, and this
record is meant to read as yours.
"""


def read(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_agents_md(agent_id):
    d = TRADER_REPO / "agents" / agent_id
    return (
        f"{read(d / 'harness.md')}\n\n{read(d / 'principles.md')}\n\n"
        f"{read(d / 'hypotheses.md')}\n\n{OPS_CONTRACT}"
    )


def recent_journal(agent_id, n=2):
    jd = TRADER_REPO / "agents" / agent_id / "journal"
    if not jd.exists():
        return ""
    files = sorted(jd.glob("*.md"), reverse=True)[:n]
    return "\n\n---\n\n".join(read(f)[-6000:] for f in files)


def market_snapshot(conn):
    rows = conn.execute(
        """select distinct on (symbol) symbol, price, prev_close, ts
           from ticks order by symbol, ts desc"""
    ).fetchall()
    lines = [f"{'SYMBOL':<10}{'PRICE':>14}{'PREV CLOSE':>14}{'CHANGE':>10}"]
    newest = None
    for r in rows:
        p, pc = float(r["price"]), float(r["prev_close"] or 0)
        chg = f"{(p / pc - 1) * 100:+.2f}%" if pc else "n/a"
        lines.append(
            f"{r['symbol']:<10}{p:>14,.2f}{(f'{pc:,.2f}' if pc else 'n/a'):>14}{chg:>10}"
        )
        newest = max(newest, r["ts"]) if newest else r["ts"]
    return "\n".join(lines), newest


def portfolio_block(conn, agent_id):
    st = conn.execute(
        "select cash, peak_equity, launched, bench from agent_state where agent_id=%s",
        (agent_id,),
    ).fetchone()
    pos = conn.execute(
        "select * from positions where agent_id=%s order by symbol", (agent_id,)
    ).fetchall()
    orders = conn.execute(
        "select id, kind, side, symbol, qty, params from orders where agent_id=%s and status='open'",
        (agent_id,),
    ).fetchall()
    prices = {
        r["symbol"]: float(r["price"])
        for r in conn.execute(
            "select distinct on (symbol) symbol, price from ticks order by symbol, ts desc"
        ).fetchall()
    }
    lines = [f"cash: ${float(st['cash']):,.2f}"]
    pv = 0.0
    for p in pos:
        px = prices.get(p["symbol"])
        val = float(p["qty"]) * px if px else None
        pv += val or 0
        pl = (px / float(p["avg_fill"]) - 1) * 100 if px else None
        lines.append(
            f"position {p['symbol']}: qty {p['qty']}, avg_fill {p['avg_fill']}, "
            f"now {px}, value ${val:,.0f} ({pl:+.1f}%) · thesis: {p['thesis']} "
            f"· invalidation: {p['invalidation']} · review_by {p['review_by']}"
        )
    eq = float(st["cash"]) + pv
    lines.append(f"equity: ${eq:,.2f} · peak: ${float(st['peak_equity']):,.2f}")
    cost = cost_line(conn, agent_id, eq)
    if cost:
        lines.append(cost)
    for o in orders:
        lines.append(
            f"standing order #{o['id']}: {o['kind']} {o['side']} {o['symbol']} "
            f"qty={o['qty'] or 'all'} params={o['params']}"
        )
    return "\n".join(lines), eq


def trading_cost(conn, agent_id, since=None):
    """What the act of trading has cost this agent: notional put through the
    market, and the frictions charged on it.

    An agent can read its cash, its positions and its P&L, but until now it
    could not read the bill for trading itself. That bill is the best
    documented way a book bleeds: in Barber and Odean's 66,465 households the
    most active traders earned 11.4% a year against the market's 17.9%, on
    stock picks that were no worse than anybody else's. The trading ate the
    difference. Measured here on 2026-07-31, maverick had put $362,014 through
    the market in nine days on a $100,000 book, paying $543, and nothing in its
    context had ever told it so.

    This is not a nudge toward caution. The mandate is unchanged and inaction
    still has to be argued for. It is the other half of a ledger the brain has
    been reasoning from with one side hidden: an agent that reads the number
    and decides its edge is worth the toll has made a real decision, and today
    it cannot even have the thought.

    Frictions are derived, never stored: core.COST is applied to the engine
    price to produce the fill price, so the toll on each fill is exactly
    qty x |fill_price - price|. Reading it back off the fills means this can
    never disagree with the book.
    """
    r = conn.execute(
        """select count(*) n,
                  coalesce(sum(qty * price), 0) notional,
                  coalesce(sum(qty * abs(fill_price - price)), 0) frictions
           from fills
           where agent_id=%s and (%s::timestamptz is null or ts > %s)""",
        (agent_id, since, since),
    ).fetchone()
    return {
        "n": int(r["n"]),
        "notional": float(r["notional"]),
        "frictions": float(r["frictions"]),
    }


def cost_line(conn, agent_id, equity, since=None, label="since you launched"):
    """One line of the book: what the trading cost, in the agent's own terms.
    None when the agent has never traded, because a row of zeroes reads as an
    accusation rather than a fact."""
    c = trading_cost(conn, agent_id, since)
    if not c["n"]:
        return None
    turns = c["notional"] / equity if equity else 0.0
    pct = c["frictions"] / equity * 100 if equity else 0.0
    return (
        f"cost of trading {label}: {c['n']} fills, ${c['notional']:,.0f} put "
        f"through the market ({turns:.1f}x your equity), ${c['frictions']:,.0f} "
        f"paid in frictions ({pct:.2f}% of your equity)"
    )


def pending_guidance(conn, agent_id):
    """Notes filed at the principal's desk that this run must answer."""
    rows = conn.execute(
        """select cid, text, filed_at from guidance
           where agent_id=%s and disposition is null order by id""",
        (agent_id,),
    ).fetchall()
    if not rows:
        return ""
    lines = [f"- {r['cid']} (filed {r['filed_at']:%Y-%m-%d}): {r['text']}" for r in rows]
    return ("\n".join(lines) +
            "\n\nEach of these needs one guidance_response this run, and a "
            "\"## Guidance\" section in your journal naming it.")


def build_task(conn, agent_id):
    snap, snap_ts = market_snapshot(conn)
    pf, equity = portfolio_block(conn, agent_id)
    trig = conn.execute(
        "select kind, details, ts from triggers_fired where agent_id=%s and not handled",
        (agent_id,),
    ).fetchall()
    trig_txt = (
        "\n".join(f"- {t['kind']} at {t['ts']}: {t['details']}" for t in trig)
        or "none"
    )
    guidance = pending_guidance(conn, agent_id)
    now = datetime.now(timezone.utc)
    return (
        f"Run your trading day. Now: {now:%Y-%m-%d %H:%M} UTC.\n\n"
        f"## Market snapshot (engine prices as of {snap_ts:%Y-%m-%d %H:%M} UTC; "
        f"fills will execute near these; this is what is quoted now, not the "
        f"universe you are confined to, see watchlist_request)\n{snap}\n\n"
        f"## Your book\n{pf}\n\n"
        f"## Events since your last run (engine triggers)\n{trig_txt}\n\n"
        + (f"## Guidance filed at your desk, unanswered\n{guidance}\n\n"
           if guidance else "")
        + f"## Your recent journal\n{recent_journal(agent_id)}\n\n"
        "Deliberate in character per your principles. Research with google_search "
        "where your rationale needs live facts (cite sources). Then emit your "
        "operations block exactly as specified. Remember: exactly one "
        "journal_entry; holding is a decision that must be argued."
    ), equity
