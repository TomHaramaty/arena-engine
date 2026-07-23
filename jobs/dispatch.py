"""Dispatch brain runs.

  python -m jobs.dispatch                  # daily: every active agent due this slot
  python -m jobs.dispatch --triggers-only  # only agents with unhandled triggers
One agent's failure never blocks the others.

Cadence tiers: house agents run both daily slots; seated (interview-born)
agents run once daily, at the close slot only. The slot comes from DAILY_SLOT
('open'|'close', set by the workflow) or is inferred from the clock — the two
Cloud Scheduler dispatches fire at 14:40 UTC (open) and 20:40 UTC (close).
"""
import os
import sys
import traceback
from datetime import datetime, timezone

from engine import db
from jobs.agent_run import run_agent


def daily_slot():
    slot = os.environ.get("DAILY_SLOT")
    if slot in ("open", "close"):
        return slot
    return "open" if datetime.now(timezone.utc).hour < 17 else "close"


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
        slot = daily_slot()
        rows = conn.execute(
            """select id from agents where status='active'
               and (coalesce(tier,'house') <> 'seated' or %s = 'close')
               order by id""",
            (slot,),
        ).fetchall()
        trigger = "scheduled"
        print(f"daily slot: {slot}")
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
