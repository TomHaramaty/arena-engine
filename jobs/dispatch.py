"""Dispatch brain runs.

  python -m jobs.dispatch                  # daily: every active agent due this slot
  python -m jobs.dispatch --triggers-only  # only agents with unhandled triggers
One agent's failure never blocks the others.

All active agents run both daily slots. The slot comes from DAILY_SLOT
('open'|'close', set by the workflow) or is inferred from the clock — the
Cloud Scheduler dispatches fire at 14:40 UTC (open) and 20:40 UTC (close),
and a GitHub cron backup fires at :50. Duplicate triggers are safe: an agent
that already has a scheduled run this slot today is skipped, so the backup is a
no-op whenever the primary fired — except for the agents whose session died
before it could touch the book, which is exactly what the backup is for.
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


#: What share of a dispatch may fail before the step itself is a failure.
#:
#: Every scheduled dispatch from 2026-08-03 to 2026-08-13 went red, on three to
#: six agents out of thirty-five, every time. A signal that fires on every
#: single run is not a signal, and the one day something else was actually
#: broken — GitHub's git operations were degraded at 14:32-14:45 UTC on
#: 2026-08-13 and the run died before a brain woke — looked exactly like the
#: eighteen days of noise before it.
#:
#: So: one flaky brain is a fact about that brain, reported to Sentry and
#: printed here, and the slot still happened. Four is a fact about the arena and
#: the step goes red. This is a threshold on the noise, not a licence to lose
#: sessions — every tolerated failure is captured, and one whose book was left
#: untouched is re-fired by the backup trigger ten minutes later.
FAILURE_TOLERANCE = 0.10


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


#: UTC hours [start, end) in which an inferred slot may run. GitHub's backup
#: cron has landed three to five hours late since September 2026: the 14:50
#: backup arrived after 17:00 and ran the close before the bell, the 20:50
#: backup arrived after midnight and ran the next day's open overnight, and on
#: 2026-10-03 it ran a full session on a Saturday. Both bells (14:40 and 20:40,
#: or 15:40 and 21:40 once US daylight time ends) sit inside these windows.
SLOT_WINDOWS = {"open": (13, 17), "close": (20, 24)}


def out_of_window(slot):
    """Why an inferred slot must not run now, or None if it may.

    An explicit DAILY_SLOT is an operator's deliberate catch-up and is never
    refused; only a slot guessed from the clock is held to its window."""
    if os.environ.get("DAILY_SLOT") in ("open", "close"):
        return None
    now = datetime.now(timezone.utc)
    start, end = SLOT_WINDOWS[slot]
    if now.weekday() >= 5 or not start <= now.hour < end:
        return (f"fired at {now:%a %H:%M} UTC, outside the {slot} window "
                f"({start:02d}:00-{end - 1:02d}:59 UTC, weekdays) — a late "
                f"backup; not running")
    return None


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
        refused = out_of_window(slot)
        if refused:
            print(f"daily slot: {slot} {refused}")
            return
        # Two guards make duplicate triggers harmless:
        # - already-ran: skip any agent with a scheduled run this slot today
        #   (a crashed 'started' run also counts — brain runs are not
        #   idempotent, so a partial run must not be blindly re-fired; its
        #   failure reaches Sentry instead). The exception is a run marked
        #   retryable, which says the session died before it could touch the
        #   book (see agent_run.untouched_book): nothing was applied, so the
        #   agent is owed the session and the backup trigger is the one thing
        #   in the system positioned to give it back. Until 2026-08-13 it did
        #   the opposite — the failed row counted as a run, so the backup
        #   skipped precisely the agents that had lost their slot, and beacon
        #   quietly lost nine sessions in twelve days.
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
                        then 'open' else 'close' end) = %s
                   and not (r.status = 'failed'
                            and coalesce((r.meta->>'retryable')::boolean, false)))
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
        except Exception as e:
            failures += 1
            conn.rollback()
            print(f"[{r['id']}] FAILED:")
            traceback.print_exc()
            obs.capture(e, agent=r["id"], trigger=trigger)
    print(f"dispatch done: {len(rows) - failures}/{len(rows)} succeeded")
    tolerated = int(len(rows) * FAILURE_TOLERANCE)
    if failures > tolerated:
        raise SystemExit(1)
    if failures:
        print(f"{failures} failed, within the tolerance of {tolerated} for "
              f"{len(rows)} agents — reported, and re-fired by the backup "
              "trigger if the book was left untouched. The step is green.")


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
