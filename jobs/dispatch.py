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


# Two bells a day, plus room for the events that matter — a stop filling, a
# position closing, a watchlist grant. Beyond this something is waking an agent
# in a loop, and the arena stops paying for it.
#
# This is a circuit breaker, not a diagnosis. The drawdown loop found on
# 2026-07-30 is fixed at its cause (engine/core.drawdown_wake), but it ran for
# two days spending brain runs and appending journal entries to an append-only
# record before anyone noticed, and nothing in the system would have stopped it.
# The next loop will have a different cause and the same shape, so the ceiling is
# on the shape.
#
# Only event dispatch is capped. The scheduled bells are the sessions the arena
# exists for, they are already once-per-slot, and an agent must never lose one
# because its morning was noisy.
MAX_EVENT_SESSIONS_PER_DAY = 6


def over_ceiling(conn, limit=MAX_EVENT_SESSIONS_PER_DAY):
    """{agent_id: sessions today} for agents that have had too many."""
    rows = conn.execute(
        """select agent_id, count(*) n from runs
           where (started at time zone 'utc')::date = (now() at time zone 'utc')::date
             and trigger <> 'reflection'
           group by agent_id having count(*) >= %s""",
        (limit,),
    ).fetchall()
    return {r["agent_id"]: r["n"] for r in rows}


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
        spent = over_ceiling(conn)
        for aid, n in sorted(spent.items()):
            print(f"[{aid}] CEILING: {n} sessions today (limit "
                  f"{MAX_EVENT_SESSIONS_PER_DAY}) — event wakes held until "
                  f"tomorrow; something is triggering in a loop")
        rows = [r for r in rows if r["id"] not in spent]
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


def reflect_due_events(conn):
    """Run the reflections an event has already made due, at tick latency.

    The reflection discipline says a closed position, a filled stop, a drawdown
    past the line or a dormancy charge each earn a reflection. reflect_run
    computed that set correctly from the beginning and nothing called it: the
    only caller was the Friday workflow, so a position closed on Monday was
    judged on Friday, four days after the fact and with three more sessions
    layered on top of it. Measured 2026-07-31, eight days in: two reflections
    had ever run outside a Friday, and half the floor had never reflected at
    all.

    This runs from the hourly tick, in the same step that wakes triggered
    agents, so it needs no new workflow and no new secret. The weekly floor
    stays where it is. Returns the number that failed.
    """
    from jobs import reflect_run   # imported here: the Pro client this pulls in
    # is dead weight on the daily dispatch, which never reflects.
    agents = reflect_run.under_ceiling(
        conn, reflect_run.due_agents(conn, weekly=False))
    print(f"event-due for reflection: {agents or 'none'}")
    failures = 0
    for aid in agents:
        try:
            reflect_run.reflect_agent(conn, aid)
        except Exception:
            failures += 1
            conn.rollback()
            print(f"[{aid}] REFLECTION FAILED:")
            traceback.print_exc()
    return failures


def main():
    obs.init("dispatch")
    triggers_only = "--triggers-only" in sys.argv
    if triggers_only:
        # The two halves are independent on purpose. A brain that could not be
        # woken must not also cost the arena the reflections that were already
        # owed, and a reflection that fails must not hide a failed wake — so
        # both always run, and either one failing fails the step.
        woke_badly = False
        try:
            dispatch(triggers_only=True)
        except SystemExit:
            woke_badly = True
        if reflect_due_events(db.connect()) or woke_badly:
            raise SystemExit(1)
        return
    # threshold=1: the trigger is already redundant (Cloud Scheduler + GH cron
    # backup), so a missed window means both failed — a lost market slot.
    with obs.cron("engine-daily-run", "40 14,20 * * 1-5",
                  checkin_margin=15, max_runtime=120, failure_issue_threshold=1):
        dispatch(triggers_only=False)


if __name__ == "__main__":
    main()
