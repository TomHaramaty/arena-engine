"""The operator's console — what the floor's System tab used to publish.

Run costs, token counts, the operations ledger and the trigger queue answer
"is everything working?" for whoever runs the arena. They answer nothing for a
visitor, and a running bill on a public page anchors a price on a product that
has not set one. So they left arena.json (2026-07-28, design/trades) and live
here instead, read straight from Postgres.

This is genuinely operator-only, which an unlisted page on open-outcry.web.app
would not have been: anything the site publishes is served to anyone who asks
for the URL, linked or not.

Usage: python3 -m jobs.ops [--runs 25] [--ops 40] [--triggers 20]
"""
import argparse
import json

from engine import db


def _t(ts):
    return ts.strftime("%b %d %H:%M") if ts else "—"


def _row(cells, widths):
    return "  ".join(str(c)[:w].ljust(w) for c, w in zip(cells, widths))


def _table(title, header, widths, rows):
    print(f"\n{title}")
    print(_row(header, widths))
    print("  ".join("─" * w for w in widths))
    if not rows:
        print("(none)")
    for r in rows:
        print(_row(r, widths))


def report(conn, n_runs=25, n_ops=40, n_trig=20):
    last = conn.execute("select max(ts) t from ticks").fetchone()["t"]
    symbols = conn.execute("select count(distinct symbol) n from ticks").fetchone()["n"]
    spend = conn.execute("select coalesce(sum(cost_usd),0) c, count(*) n from runs").fetchone()
    active = conn.execute("select count(*) n from agents where status='active'").fetchone()["n"]
    pending = conn.execute(
        "select count(*) n from triggers_fired where not handled"
    ).fetchone()["n"]
    unanswered = conn.execute(
        "select count(*) n from guidance where disposition is null"
    ).fetchone()["n"]

    print("OPEN OUTCRY — operator console")
    print(f"  prices          {_t(last)} · {symbols} symbols tracked")
    print(f"  active traders  {active}")
    print(f"  brain runs      {spend['n']}")
    print(f"  total spend     ${float(spend['c']):.2f}  (all time, all traders)")
    print(f"  pending wakes   {pending}")
    print(f"  desk notes out  {unanswered} unanswered")

    runs = conn.execute(
        "select * from runs order by started desc limit %s", (n_runs,)
    ).fetchall()
    _table("BRAIN RUNS", ["started", "trader", "trigger", "status", "cost", "tok in", "tok out"],
           [13, 10, 12, 10, 8, 8, 8],
           [[_t(r["started"]), r["agent_id"], r["trigger"], r["status"],
             f"${float(r['cost_usd']):.3f}" if r["cost_usd"] is not None else "—",
             r["tokens_in"] if r["tokens_in"] is not None else "—",
             r["tokens_out"] if r["tokens_out"] is not None else "—"] for r in runs])

    ops = conn.execute(
        """select o.*, r.agent_id from operations o join runs r on r.id=o.run_id
           order by o.created_at desc limit %s""", (n_ops,)
    ).fetchall()
    _table("OPERATIONS", ["when", "trader", "op", "verdict", "detail"],
           [13, 10, 24, 9, 62],
           [[_t(o["created_at"]), o["agent_id"], o["type"], o["verdict"],
             o["reason"] or json.dumps(o["payload"])] for o in ops])

    trig = conn.execute(
        "select * from triggers_fired order by ts desc limit %s", (n_trig,)
    ).fetchall()
    _table("TRIGGERS", ["when", "trader", "kind", "state", "detail"],
           [13, 10, 18, 9, 62],
           [[_t(t["ts"]), t["agent_id"], t["kind"],
             "handled" if t["handled"] else "PENDING",
             json.dumps(t["details"])] for t in trig])

    rejected = conn.execute(
        """select o.reason, count(*) n from operations o
           where o.verdict='rejected' group by 1 order by n desc limit 15"""
    ).fetchall()
    _table("REJECTIONS BY REASON", ["n", "reason"], [4, 96],
           [[r["n"], r["reason"] or "—"] for r in rejected])
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=25)
    ap.add_argument("--ops", type=int, default=40)
    ap.add_argument("--triggers", type=int, default=20)
    a = ap.parse_args()
    report(db.connect(), a.runs, a.ops, a.triggers)


if __name__ == "__main__":
    main()
