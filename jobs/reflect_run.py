"""Run reflections.

  python -m jobs.reflect_run <agent_id>       # reflect one agent now
  python -m jobs.reflect_run --due            # event-due agents + Friday floor
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

from engine import db
from engine import observability as obs
from runner import context, reflect

# dormant: the agent has stopped acting. Inaction is a decision the
# reflection must judge like any other — and it is the ONLY reflection
# path for an agent whose entry conditions never fire, since it closes
# no positions and takes no drawdown.
REFLECT_KINDS = ("position_closed", "stop_filled", "drawdown", "dormant")


def due_agents(conn):
    now = datetime.now(timezone.utc)
    out = set()
    for r in conn.execute("select id from agents where status='active'").fetchall():
        since = reflect.last_reflection_ts(conn, r["id"])
        ev = conn.execute(
            "select 1 from triggers_fired where agent_id=%s and kind = any(%s) and ts>%s limit 1",
            (r["id"], list(REFLECT_KINDS), since),
        ).fetchone()
        week_floor = now.weekday() == 4 and (now - _as_dt(since)).days >= 5
        if ev or week_floor:
            out.add(r["id"])
    return sorted(out)


def _as_dt(x):
    if isinstance(x, datetime):
        return x
    return datetime(x.year, x.month, x.day, tzinfo=timezone.utc)


def commit_prose(agent_id, date_str):
    repo = str(context.TRADER_REPO)
    subprocess.run(["git", "-C", repo, "add", f"agents/{agent_id}"], check=True)
    r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"])
    if r.returncode != 0:
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m",
                        f"reflection({agent_id}): {date_str}"], check=True)
        subprocess.run(["git", "-C", repo, "push", "-q"], check=True)


def reflect_agent(conn, agent_id):
    run_id = conn.execute(
        "insert into runs (agent_id, trigger) values (%s,'reflection') returning id",
        (agent_id,),
    ).fetchone()["id"]
    conn.commit()
    prompt = reflect.build_reflection_prompt(conn, agent_id)
    print(f"[{agent_id}] reflection prompt: {len(prompt)} chars")
    R, tin, tout, cost = reflect.call_pro(prompt)
    print(f"[{agent_id}] pro call: in {tin} / out {tout} → ${cost}")
    summary = reflect.apply_reflection(conn, agent_id, run_id, R)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from jobs.agent_run import commit_journal  # journal file + push
    title = "REFLECTION — " + (R.get("journal_title") or "scheduled reflection")
    body = R.get("journal_body", "")
    if R.get("counterargument") and "counterargument" not in body.lower():
        body += f"\n\n## Counterargument\n{R['counterargument']}"
    commit_journal(agent_id, title, body, date_str)
    commit_prose(agent_id, date_str)

    conn.execute(
        """update runs set status='completed', finished=now(), cost_usd=%s,
           tokens_in=%s, tokens_out=%s, meta=%s where id=%s""",
        (cost, tin, tout, json.dumps({"changes": summary,
                                      "verdicts": len(R.get("verdicts", []))}), run_id),
    )
    conn.commit()
    print(f"[{agent_id}] reflection complete — changes: {summary or ['none — evidence recorded only']}")


def _run(conn, agents):
    failures = 0
    for aid in agents:
        try:
            reflect_agent(conn, aid)
        except Exception:
            failures += 1
            conn.rollback()
            import traceback
            print(f"[{aid}] REFLECTION FAILED:")
            traceback.print_exc()
    if failures:
        raise SystemExit(1)


def main():
    obs.init("reflect")
    conn = db.connect()
    if "--due" in sys.argv:
        # --due is idempotent: a completed reflection advances
        # last_reflection_ts, so the week floor and event window both close.
        # Duplicate Friday triggers (Cloud Scheduler 21:30 + GH cron 21:45)
        # therefore no-op. Only Friday runs check in to the cron monitor —
        # a manual --due on another day would otherwise log off-window.
        if datetime.now(timezone.utc).weekday() == 4:
            with obs.cron("engine-reflect", "30 21 * * 5",
                          checkin_margin=20, max_runtime=60,
                          failure_issue_threshold=1):
                agents = due_agents(conn)
                print(f"due for reflection: {agents or 'none'}")
                _run(conn, agents)
            return
        agents = due_agents(conn)
        print(f"due for reflection: {agents or 'none'}")
    else:
        agents = [a for a in sys.argv[1:] if not a.startswith("--")]
    _run(conn, agents)


if __name__ == "__main__":
    main()
