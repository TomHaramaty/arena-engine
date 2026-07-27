"""Add any seed-universe symbol missing from the watchlist, properly resolved.

The universe is open — an agent can request anything quotable — but the seed
list is still what a newborn sees at its first bell, and what the seat
interview validates a benchmark against. When that list grows (jobs/seed.py's
UNIVERSE), this carries the new names in: resolved against the data source,
stored under the source symbol they actually quote under, and classified, so
class caps bind them from the first tick.

Never removes or rewrites an existing row — see jobs/classify_watchlist.py for
reclassifying what is already listed.

Usage: python -m jobs.sync_universe [--dry]
"""
import sys
import time

from engine import db, marketdata
from jobs.seed import CRYPTO, UNIVERSE


def main():
    dry = "--dry" in sys.argv
    conn = db.connect()
    db.migrate(conn)

    listed = {
        r["symbol"]
        for r in conn.execute("select symbol from watchlist").fetchall()
    }
    missing = [s for s in list(UNIVERSE) + list(CRYPTO) if s not in listed]
    if not missing:
        print("watchlist already carries the whole seed universe")
        return

    added, rejected, unreachable = 0, [], []
    for i, sym in enumerate(missing):
        if i and i % 25 == 0:
            time.sleep(60)  # two calls per symbol; stay under 60/min
        try:
            res = marketdata.resolve(sym)
        except marketdata.QuoteError as e:
            unreachable.append(f"{sym} ({e})")
            continue
        if not res:
            rejected.append(sym)
            continue
        print(f"+ {sym:10} {res['asset_class']:16} {res['source_symbol']:20} "
              f"{res['description'][:34]}")
        added += 1
        if not dry:
            conn.execute(
                """insert into watchlist (symbol, source_symbol, asset_class,
                                          description, requested_by, status)
                   values (%s,%s,%s,%s,'seed','active') on conflict do nothing""",
                (sym, res["source_symbol"], res["asset_class"], res["description"]),
            )
    if not dry:
        conn.commit()

    print(f"\n{added}/{len(missing)} {'would be ' if dry else ''}added")
    if rejected:
        print(f"NOT QUOTABLE (not added): {', '.join(rejected)}")
    if unreachable:
        print(f"UNREACHABLE (not a verdict — re-run): {', '.join(unreachable)}")
    counts = conn.execute(
        "select asset_class, count(*) n from watchlist where status='active'"
        " group by 1 order by 2 desc"
    ).fetchall()
    print("classes: " + ", ".join(f"{c['asset_class']}={c['n']}" for c in counts))


if __name__ == "__main__":
    main()