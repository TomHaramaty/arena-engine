"""One-time migration: seed the DB from the v1 trader-arena repo state.

Usage: python -m jobs.seed [--force]   (TRADER_REPO env or ../trader)
"""
import json
import os
import pathlib
import sys

from engine import db

UNIVERSE = (
    "SPY QQQ IWM ACWI DIA XLK XLF XLE XLV XLY XLP XLI XLU SMH XBI AAPL MSFT "
    "NVDA GOOGL AMZN META TSLA AVGO AMD MU TSM ORCL PLTR COIN HOOD CRWD SNOW "
    "NFLX UBER SHOP JPM GS V MA XOM CVX LLY UNH JNJ PFE MRK KO PG WMT COST "
    "HD CAT BA TLT IEF GLD SLV USO"
).split()
CRYPTO = {"BTC-USD": "BINANCE:BTCUSDT", "ETH-USD": "BINANCE:ETHUSDT"}

AGENTS = {
    "tempo": {
        "name": "Tempo", "archetype": "Momentum rider", "brain": "routine-claude",
        "config": {"max_single_pct": 0.25, "cluster_cap_pct": 0.40},
    },
    "catalyst": {
        "name": "Catalyst", "archetype": "Event-driven", "brain": "routine-claude",
        "config": {"max_single_pct": 0.20, "max_positions": 6},
    },
    "vertex": {
        "name": "Vertex", "archetype": "Concentrated growth", "brain": "routine-claude",
        "config": {"max_single_pct": 0.35, "min_positions": 3, "max_positions": 5},
    },
    "maverick": {
        "name": "Maverick", "archetype": "Aggressive contrarian", "brain": "routine-claude",
        "config": {"max_single_pct": 0.25},
    },
    "wildcat": {
        "name": "Wildcat", "archetype": "Cross-asset opportunist",
        "brain": "antigravity-gemini",  # v2 pilot
        "config": {"crypto_core_cap_pct": 0.50, "max_single_equity_pct": 0.20},
    },
}

# Hard standing orders carried over from v1 principles (registered on the
# agents' behalf at migration; noted in their next journal by the runner).
STANDING_ORDERS = [
    # Tempo P3: trailing stop −10%, honored same session — now broker-guaranteed.
    {"agent_id": "tempo", "kind": "trailing_stop", "side": "sell", "symbol": "AMD",
     "qty": None, "params": {"trail_pct": 0.10, "high_water": 557.91},
     "reason": "P3 hard rule — registered at v2 migration"},
]


def main():
    force = "--force" in sys.argv
    repo = pathlib.Path(os.environ.get("TRADER_REPO", "../trader"))
    conn = db.connect()
    db.migrate(conn)

    n = conn.execute("select count(*) c from agents").fetchone()["c"]
    if n and not force:
        print(f"agents table already has {n} rows — pass --force to reseed. Aborting.")
        return

    for sym in UNIVERSE:
        conn.execute(
            """insert into watchlist (symbol, source_symbol, requested_by)
               values (%s,%s,'seed') on conflict do nothing""", (sym, sym))
    for sym, src in CRYPTO.items():
        conn.execute(
            """insert into watchlist (symbol, source_symbol, requested_by)
               values (%s,%s,'seed') on conflict do nothing""", (sym, src))

    for aid, meta in AGENTS.items():
        pf = json.loads((repo / "agents" / aid / "portfolio.json").read_text())
        conn.execute(
            """insert into agents (id, name, archetype, brain, config)
               values (%s,%s,%s,%s,%s)
               on conflict (id) do update set brain=excluded.brain, config=excluded.config""",
            (aid, meta["name"], meta["archetype"], meta["brain"], json.dumps(meta["config"])))
        bench = {
            "symbols": pf["benchmark"]["symbols"],
            "weights": pf["benchmark"]["weights"],
            "launch_prices": pf["benchmark"]["launch_prices"],
        }
        conn.execute(
            """insert into agent_state (agent_id, cash, peak_equity, launched, bench)
               values (%s,%s,%s,%s,%s)
               on conflict (agent_id) do update set cash=excluded.cash,
                 peak_equity=excluded.peak_equity, launched=excluded.launched,
                 bench=excluded.bench""",
            (aid, pf["cash"], pf["peak_equity"], pf["launched"], json.dumps(bench)))
        for p in pf.get("positions", []):
            conn.execute(
                """insert into positions (agent_id, symbol, qty, avg_fill, opened_at,
                                          thesis, invalidation, review_by)
                   values (%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (agent_id, symbol) do nothing""",
                (aid, p["symbol"], p["qty"], p["fill_price"], p["entry_date"],
                 p.get("thesis", ""), p.get("invalidation", ""), p.get("review_by")))

    for so in STANDING_ORDERS:
        conn.execute(
            """insert into orders (agent_id, kind, side, symbol, qty, params, reason)
               values (%s,%s,%s,%s,%s,%s,%s)""",
            (so["agent_id"], so["kind"], so["side"], so["symbol"], so["qty"],
             json.dumps(so["params"]), so["reason"]))

    conn.commit()
    for row in conn.execute(
        """select a.id, a.brain, s.cash, s.launched,
                  (select count(*) from positions p where p.agent_id=a.id) n_pos
           from agents a join agent_state s on s.agent_id=a.id order by a.id"""
    ).fetchall():
        print(row)
    print("seeded.")


if __name__ == "__main__":
    main()
