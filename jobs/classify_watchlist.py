"""Backfill asset_class / source_symbol / description on existing watchlist rows.

The 60 seed symbols predate symbol resolution: they were inserted with
source_symbol = symbol and no class, so the schema default ('equity') would
call SPY, GLD and BTC-USD equities — and per-agent class caps are enforced
against that column. Resolves every active row against the data source and
writes back what it actually is.

Idempotent; safe to re-run. A row that no longer resolves is reported and left
untouched (it is already quoting, or it is a symbol worth a human's attention —
neither is something a backfill should silently delete).

Each row costs two API calls, so this paces itself under the free tier's 60/min
rather than tripping the rate limit and misreading 429s as bad symbols.

Usage: python -m jobs.classify_watchlist [--dry]
"""
import sys
import time

from engine import db, marketdata


def main():
    dry = "--dry" in sys.argv
    conn = db.connect()
    db.migrate(conn)

    rows = conn.execute(
        "select symbol, source_symbol, asset_class, description from watchlist"
        " where status='active' order by symbol"
    ).fetchall()

    changed, unresolved, unreachable = 0, [], []
    for i, r in enumerate(rows):
        if i and i % 25 == 0:
            time.sleep(60)  # two calls per row; stay under 60/min
        try:
            res = marketdata.resolve(r["symbol"])
        except marketdata.QuoteError as e:
            unreachable.append(f"{r['symbol']} ({e})")
            continue
        if not res:
            unresolved.append(r["symbol"])
            continue
        if (r["source_symbol"], r["asset_class"], r["description"]) == (
            res["source_symbol"], res["asset_class"], res["description"]
        ):
            continue
        print(f"{r['symbol']:10} {r['asset_class']:16} -> {res['asset_class']:16} "
              f"{res['source_symbol']:20} {res['description'][:34]}")
        changed += 1
        if not dry:
            conn.execute(
                """update watchlist set source_symbol=%s, asset_class=%s, description=%s
                   where symbol=%s""",
                (res["source_symbol"], res["asset_class"], res["description"],
                 r["symbol"]),
            )
    if not dry:
        conn.commit()

    print(f"\n{changed}/{len(rows)} rows {'would be ' if dry else ''}updated")
    if unresolved:
        print(f"UNRESOLVED (left as-is, check by hand): {', '.join(unresolved)}")
    if unreachable:
        print(f"UNREACHABLE (not a verdict — re-run): {', '.join(unreachable)}")
    counts = conn.execute(
        "select asset_class, count(*) n from watchlist where status='active'"
        " group by 1 order by 2 desc"
    ).fetchall()
    print("classes: " + ", ".join(f"{c['asset_class']}={c['n']}" for c in counts))


if __name__ == "__main__":
    main()