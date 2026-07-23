"""Run one agent's brain end-to-end: context → Antigravity → validate → apply →
journal to git → runs row.

Usage: python -m jobs.agent_run <agent_id> [--dry-run] [--trigger scheduled|manual|...]
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

from engine import db
from runner import brain, context, ops


def commit_journal(agent_id, title, body_md, date_str):
    title = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*[—-]+\s*", "", title or "run")
    path = context.TRADER_REPO / "agents" / agent_id / "journal" / f"{date_str}.md"
    header = f"# {date_str} — {title}\n\n"
    if path.exists():  # append-only: same-day reruns append, never overwrite
        content = path.read_text() + f"\n\n---\n\n{header}{body_md}\n"
    else:
        content = f"{header}{body_md}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    repo = str(context.TRADER_REPO)
    subprocess.run(["git", "-C", repo, "add", str(path)], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", f"journal({agent_id}): {date_str} run"],
        check=True,
    )
    subprocess.run(["git", "-C", repo, "push", "-q"], check=True)


def run_agent(conn, agent_id, trigger="scheduled", dry=False):
    agent = conn.execute("select * from agents where id=%s", (agent_id,)).fetchone()
    if not agent:
        raise SystemExit(f"unknown agent {agent_id}")

    run_id = None
    if not dry:
        run_id = conn.execute(
            "insert into runs (agent_id, trigger) values (%s,%s) returning id",
            (agent_id, trigger),
        ).fetchone()["id"]
        conn.commit()

    agents_md = context.build_agents_md(agent_id)
    task, equity = context.build_task(conn, agent_id)
    print(f"[{agent_id}] context: persona {len(agents_md)} chars, task {len(task)} chars, equity ${equity:,.2f}")

    text, usage, iid = brain.run(agents_md, task)
    cost = brain.cost_usd(usage)
    print(f"[{agent_id}] interaction {iid} — in {usage.get('total_input_tokens')} / "
          f"out {usage.get('total_output_tokens')} / thought {usage.get('total_thought_tokens')} "
          f"→ ${cost}")

    parsed = ops.parse(text)
    results = ops.validate_and_apply(conn, agent, run_id, parsed, dry=dry)
    for op, verdict, reason in results:
        print(f"  {verdict.upper():8s} {op.get('type'):24s} {reason or ''}")

    journal_op = next(o for o, v, _ in results if o.get("type") == "journal_entry" and v == "accepted")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not dry:
        commit_journal(agent_id, journal_op.get("title", "run"), journal_op.get("body_markdown", ""), date_str)
        conn.execute(
            """update runs set status='completed', finished=now(), cost_usd=%s,
               tokens_in=%s, tokens_out=%s, meta=%s where id=%s""",
            (cost, usage.get("total_input_tokens"),
             usage.get("total_output_tokens", 0) + usage.get("total_thought_tokens", 0),
             json.dumps({"interaction_id": iid,
                         "ops": [{"type": o.get("type"), "verdict": v} for o, v, _ in results]}),
             run_id),
        )
        conn.execute(
            "update triggers_fired set handled=true where agent_id=%s and not handled",
            (agent_id,),
        )
        conn.commit()
        print(f"[{agent_id}] run {run_id} complete — journal committed, cost ${cost}")
    else:
        print(f"[{agent_id}] DRY RUN — nothing applied. Journal preview:\n")
        print(journal_op.get("body_markdown", "")[:1500])
    return run_id


def main():
    agent_id = sys.argv[1]
    dry = "--dry-run" in sys.argv
    trigger = "manual"
    if "--trigger" in sys.argv:
        trigger = sys.argv[sys.argv.index("--trigger") + 1]
    conn = db.connect()
    run_agent(conn, agent_id, trigger=trigger, dry=dry)


if __name__ == "__main__":
    main()
