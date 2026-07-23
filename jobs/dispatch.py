"""Dispatch brain runs.

  python -m jobs.dispatch                  # daily: every active agent
  python -m jobs.dispatch --triggers-only  # only agents with unhandled triggers
One agent's failure never blocks the others.
"""
import sys
import traceback

from engine import db
from jobs.agent_run import run_agent


def main():
    triggers_only = "--triggers-only" in sys.argv
    conn = db.connect()
    if triggers_only:
        rows = conn.execute(
            """select distinct a.id from agents a
               join triggers_fired t on t.agent_id = a.id and not t.handled
               where a.status='active'"""
        ).fetchall()
        trigger = "event"
    else:
        rows = conn.execute(
            "select id from agents where status='active' order by id"
        ).fetchall()
        trigger = "scheduled"
    if not rows:
        print("no agents due.")
        return
    failures = 0
    for r in rows:
        try:
            run_agent(conn, r["id"], trigger=trigger)
        except Exception:
            failures += 1
            conn.rollback()
            print(f"[{r['id']}] FAILED:")
            traceback.print_exc()
    print(f"dispatch done: {len(rows) - failures}/{len(rows)} succeeded")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
