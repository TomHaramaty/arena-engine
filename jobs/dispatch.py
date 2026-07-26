"""Dispatch brain runs.

  python -m jobs.dispatch                  # daily: every active agent due this slot
  python -m jobs.dispatch --triggers-only  # only agents with unhandled triggers
One agent's failure never blocks the others.

All active agents run both daily slots. The slot comes from DAILY_SLOT
('open'|'close', set by the workflow) or is inferred from the clock — the
Cloud Scheduler dispatches fire at 14:40 UTC (open) and 20:40 UTC (close),
and a GitHub cron backup fires at :50. Duplicate triggers are safe: an agent
that already has a scheduled run this slot today is skipped, so the backup
is a no-op whenever the primary fired.
"""
import os
import sys
import traceback
from datetime import datetime, timezone

from engine import db
from engine import observability as obs
from jobs.agent_run import run_agent


def daily_slot():
    slot = os.environ.get("DAILY_SLOT")
    if slot in ("open", "close"):
        return slot
    return "open" if datetime.now(timezone.utc).hour < 17 else "close"


def dispatch(triggers_only):
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
        # Two guards make duplicate triggers harmless:
        # - already-ran: skip any agent with a scheduled run this slot today
        #   (a crashed 'started' run also counts — brain runs are not
        #   idempotent, so a partial run must not be blindly re-fired; its
        #   failure reaches Sentry instead).
        # - first-bell: if the principal rang the first bell today
        #   (jobs/agent_run --trigger first-bell), later slots skip the
        #   newborn rather than running it twice on day one.
        rows = conn.execute(
            """select id from agents a where status='active'
               and not exists (
                 select 1 from runs r where r.agent_id = a.id
                   and r.trigger = 'scheduled'
                   and (r.started at time zone 'utc')::date = current_date
                   and (case when extract(hour from r.started at time zone 'utc') < 17
                        then 'open' else 'close' end) = %s)
               and not (coalesce(tier,'house') = 'seated' and exists (
                 select 1 from runs r where r.agent_id = a.id
                   and r.trigger = 'first-bell'
                   and r.started::date = current_date))
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


def main():
    obs.init("dispatch")
    triggers_only = "--triggers-only" in sys.argv
    if triggers_only:
        dispatch(triggers_only=True)
        return
    # threshold=1: the trigger is already redundant (Cloud Scheduler + GH cron
    # backup), so a missed window means both failed — a lost market slot.
    with obs.cron("engine-daily-run", "40 14,20 * * 1-5",
                  checkin_margin=15, max_runtime=120, failure_issue_threshold=1):
        dispatch(triggers_only=False)


if __name__ == "__main__":
    main()
