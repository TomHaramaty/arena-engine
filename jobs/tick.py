"""Hourly tick: fetch quotes → ticks; evaluate standing orders; mark portfolios;
detect triggers. Independent of any brain run."""
from engine import core, db, marketdata


def main():
    conn = db.connect()
    db.migrate(conn)

    rows = conn.execute(
        "select symbol, source_symbol from watchlist where status='active'"
    ).fetchall()
    symbol_map = {r["symbol"]: r["source_symbol"] for r in rows}
    # benchmarks & open positions must always be quoted even if not on watchlist
    for r in conn.execute("select bench from agent_state").fetchall():
        for s in r["bench"]["symbols"]:
            symbol_map.setdefault(s, "BINANCE:BTCUSDT" if s == "BTC-USD"
                                  else "BINANCE:ETHUSDT" if s == "ETH-USD" else s)
    for r in conn.execute("select distinct symbol from positions").fetchall():
        symbol_map.setdefault(r["symbol"], r["symbol"])

    quotes = marketdata.fetch_quotes(symbol_map)
    print(f"quotes: {len(quotes)}/{len(symbol_map)}")
    if not quotes:
        raise SystemExit("no quotes fetched — aborting tick (no marks, no fills)")

    core.insert_ticks(conn, quotes)
    filled = core.evaluate_standing_orders(conn, quotes)
    for o, price, qty in filled:
        print(f"STANDING ORDER FILLED: {o['agent_id']} {o['kind']} {o['side']} "
              f"{qty} {o['symbol']} @ {price}")
    marked, skipped = core.mark_all(conn, quotes)
    for aid, eq, bidx in marked:
        print(f"mark {aid}: equity {eq} bench_idx {bidx}")
    for aid, missing in skipped:
        print(f"SKIPPED mark {aid}: missing quotes for {missing}")
    n = conn.execute(
        "select count(*) c from triggers_fired where not handled"
    ).fetchone()["c"]
    print(f"unhandled triggers: {n}")


if __name__ == "__main__":
    main()
