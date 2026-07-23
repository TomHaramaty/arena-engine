"""Builds an agent's persona (AGENTS.md) and daily task prompt from repo prose + DB state."""
import os
import pathlib
from datetime import datetime, timezone

TRADER_REPO = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))

OPS_CONTRACT = """
## How you act: operations (MANDATORY format)

You never execute trades yourself — you propose typed operations and a
deterministic engine validates and executes them. Your constitution is enforced
in code: operations that violate it are REJECTED and logged. Cash can never go
negative; fills cost 0.15% against you; fills execute at the engine's latest
price, which is provided in your market snapshot.

End your final message with exactly one fenced json block:

```json
{"operations": [
  {"type": "journal_entry", "title": "<one line>", "body_markdown": "<your full journal entry: ## Data used / ## Rationale / ## Actions / ## Hypothesis observations>"},
  {"type": "place_order", "side": "buy|sell", "symbol": "TICKER", "notional_usd": 20000, "thesis": "<why + what would prove you wrong>", "invalidation": "<explicit condition>", "review_by": "YYYY-MM-DD"},
  {"type": "register_standing_order", "kind": "stop|trailing_stop|limit", "side": "sell|buy", "symbol": "TICKER", "qty": null, "trigger_price": 0, "trail_pct": 0.10, "limit_price": 0, "note": "<which principle mandates this>"},
  {"type": "cancel_order", "order_id": 123, "note": "..."},
  {"type": "hypothesis_op", "op": "update_evidence|propose|falsify|promote|expire", "id": "H1", "evidence_for": 0, "evidence_against": 0, "note": "..."},
  {"type": "watchlist_request", "symbol": "TICKER", "note": "<why you need it>"}
]}
```

Rules:
- Exactly ONE journal_entry op per run — always, even on a hold day.
- place_order uses notional_usd (buys) or qty (sells); sells of a full position may pass "qty": "all".
- Only symbols from your market snapshot (or a watchlist_request first — grants apply NEXT run).
- Every buy needs thesis + invalidation + review_by.
- Standing orders persist and are executed mechanically by the engine at hourly ticks — this is how hard stop rules are guaranteed.
- Integrity: cite research sources in the journal; never invent data; decision quality is judged against what was knowable now.
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
            f"now {px}, value ${val:,.0f} ({pl:+.1f}%) — thesis: {p['thesis']} "
            f"— invalidation: {p['invalidation']} — review_by {p['review_by']}"
        )
    eq = float(st["cash"]) + pv
    lines.append(f"equity: ${eq:,.2f} · peak: ${float(st['peak_equity']):,.2f}")
    for o in orders:
        lines.append(
            f"standing order #{o['id']}: {o['kind']} {o['side']} {o['symbol']} "
            f"qty={o['qty'] or 'all'} params={o['params']}"
        )
    return "\n".join(lines), eq


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
    now = datetime.now(timezone.utc)
    return (
        f"Run your trading day. Now: {now:%Y-%m-%d %H:%M} UTC.\n\n"
        f"## Market snapshot (engine prices as of {snap_ts:%Y-%m-%d %H:%M} UTC — "
        f"fills will execute near these)\n{snap}\n\n"
        f"## Your book\n{pf}\n\n"
        f"## Events since your last run (engine triggers)\n{trig_txt}\n\n"
        f"## Your recent journal\n{recent_journal(agent_id)}\n\n"
        "Deliberate in character per your principles. Research with google_search "
        "where your rationale needs live facts (cite sources). Then emit your "
        "operations block exactly as specified. Remember: exactly one "
        "journal_entry; holding is a decision that must be argued."
    ), equity
