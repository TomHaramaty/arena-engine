"""Run one agent's brain end-to-end: context → Antigravity → validate → apply →
journal to git → runs row.

Usage: python -m jobs.agent_run <agent_id> [--dry-run] [--trigger scheduled|manual|...]
"""
import json
import re
import sys
import traceback
from datetime import datetime, timezone

from engine import core, db, gitrepo
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
    gitrepo.commit_and_push(
        context.TRADER_REPO, [path], f"journal({agent_id}): {date_str} run"
    )


def mark_failed(conn, run_id, exc):
    """A session that died says so on its own row.

    A run left in 'started' cannot be told apart from one still in progress, and
    the dispatcher counts it as having run (brain runs are not idempotent, so a
    partial session is deliberately never re-fired). Seven such rows had
    accumulated by 2026-07-30 with nothing on them to say what happened. The
    failure is a fact about the record and belongs in it.
    """
    conn.rollback()          # the failing statement may have poisoned the tx
    try:
        conn.execute(
            """update runs set status='failed', finished=now(),
               meta = meta || %s::jsonb where id=%s and status='started'""",
            (json.dumps({"error": f"{type(exc).__name__}: {exc}"[:500]}), run_id),
        )
        conn.commit()
    except Exception:        # never let the epitaph bury the exception itself
        conn.rollback()
        traceback.print_exc()


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
    try:
        return _session(conn, agent, agent_id, run_id, dry)
    except Exception as e:
        if run_id is not None:
            mark_failed(conn, run_id, e)
        raise


#: How many times a session may be asked for its operations. Two, not more:
#: this exists for a formatting flake, and a brain that cannot produce a
#: readable block twice is telling us something we should not paper over.
DELIBERATION_ATTEMPTS = 2


def _deliberate(agent_id, agents_md, task, run=None, parse=None):
    """Run the brain until its operations can actually be read. Returns
    (operations, tokens, total_cost_usd), where tokens are summed across every
    attempt and carry the id of the interaction that was finally read.

    An unreadable operations block used to kill the session outright. That was
    right when nothing could be said about what the brain had done; but at
    THIS point nothing has been applied, nothing has been journalled, and the
    run row holds no decisions — the same state in which brain.py already
    retries a refused POST, and for the same reason: asking the question again
    is not repairing an answer. It cost a real principal their trader's first
    session on 2026-07-31, twice, on a block that parsed perfectly the next
    time it was asked.

    Every attempt's tokens are paid for and counted, so the recorded spend
    stays true. A brain that cannot emit a readable block twice raises, and
    the session fails as it always did.
    """
    run = run or brain.run
    parse = parse or ops.parse
    cost, tokens_in, tokens_out = 0.0, 0, 0
    for attempt in range(1, DELIBERATION_ATTEMPTS + 1):
        text, usage, iid = run(agents_md, task)
        cost += brain.cost_usd(usage)
        tokens_in += usage.get("total_input_tokens", 0)
        tokens_out += (usage.get("total_output_tokens", 0)
                       + usage.get("total_thought_tokens", 0))
        print(f"[{agent_id}] interaction {iid} — in {usage.get('total_input_tokens')} / "
              f"out {usage.get('total_output_tokens')} / thought {usage.get('total_thought_tokens')} "
              f"→ ${round(cost, 4)}")
        try:
            return (parse(text),
                    {"in": tokens_in, "out": tokens_out, "interaction_id": iid},
                    round(cost, 4))
        except ops.OpsParseError as e:
            if attempt == DELIBERATION_ATTEMPTS:
                raise
            print(f"[{agent_id}] the operations block could not be read ({e}). "
                  "Nothing has been applied; asking again.")
    raise AssertionError("unreachable")


def _session(conn, agent, agent_id, run_id, dry):
    agents_md = context.build_agents_md(agent_id)
    task, equity = context.build_task(conn, agent_id)
    print(f"[{agent_id}] context: persona {len(agents_md)} chars, task {len(task)} chars, equity ${equity:,.2f}")

    parsed, spent, cost = _deliberate(agent_id, agents_md, task)
    results = ops.validate_and_apply(conn, agent, run_id, parsed, dry=dry)
    for op, verdict, reason in results:
        print(f"  {verdict.upper():8s} {op.get('type'):24s} {reason or ''}")

    # A note filed at the desk that this run did not answer stays pending and is
    # put in front of the agent again next session — never silently dropped.
    unanswered = [r["cid"] for r in conn.execute(
        "select cid from guidance where agent_id=%s and disposition is null order by id",
        (agent_id,)).fetchall()]
    if unanswered:
        print(f"[{agent_id}] GUIDANCE UNANSWERED: {', '.join(unanswered)} "
              "— carried to the next session")

    journal_op = next(o for o, v, _ in results if o.get("type") == "journal_entry" and v == "accepted")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title, body = journal_op.get("title", "run"), journal_op.get("body_markdown", "")
    if not dry:
        # The entry lands in Postgres BEFORE it is pushed to git. The operations
        # above are already committed — the book has moved — so if the push then
        # fails for good (a rejected non-fast-forward, a dead network), the
        # explanation for those trades must still exist somewhere to be filed
        # from. Without this the record simply loses the entry, since a crashed
        # run is deliberately never re-fired. jobs/doctor.py reads these back
        # and reports any that never reached the record.
        conn.execute(
            "update runs set meta = meta || %s::jsonb where id=%s",
            (json.dumps({"journal": {"date": date_str, "title": title,
                                     "body_markdown": body}}), run_id),
        )
        conn.commit()
        commit_journal(agent_id, title, body, date_str)
        # `meta - 'journal'`: the entry is in the record now, which is where
        # published prose belongs — the Postgres copy was a lifeline for a push
        # that failed, and a completed run does not need two copies. What is
        # left behind is exactly the recoverable set doctor looks for.
        conn.execute(
            """update runs set status='completed', finished=now(), cost_usd=%s,
               tokens_in=%s, tokens_out=%s, meta=(meta - 'journal') || %s::jsonb
               where id=%s""",
            (cost, spent["in"], spent["out"],
             json.dumps({"interaction_id": spent["interaction_id"],
                         "ops": [{"type": o.get("type"), "verdict": v} for o, v, _ in results]}),
             run_id),
        )
        conn.execute(
            "update triggers_fired set handled=true where agent_id=%s and not handled",
            (agent_id,),
        )
        conn.commit()
        print(f"[{agent_id}] run {run_id} complete — journal committed, cost ${cost}")
        # Marked handled above, so a dormancy flag filed now survives this run
        # and stands until the reflection that answers it.
        streak = core.flag_dormancy(conn, agent_id)
        if streak:
            print(f"[{agent_id}] DORMANT: {streak} sessions without an order "
                  f"— reflection now due")
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
