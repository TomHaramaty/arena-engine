"""Reflection engine: deep periodic self-examination on Gemini Pro (no sandbox —
pure reasoning). Produces two-ledger verdicts, hypothesis evidence updates,
principle changes — and applies them to the prose files."""
import json
import os
import re
from datetime import datetime, timezone

import requests

from runner import context, prose

MODEL = "gemini-3.1-pro-preview"
RATE_IN, RATE_OUT = 2.00 / 1e6, 12.00 / 1e6

SCHEMA = """{
 "verdicts": [{"decision": "<the decision reviewed>", "quality": "good|bad",
               "outcome": "good|bad|pending", "reasoning": "<judged against what was knowable THEN>"}],
 "hypothesis_updates": [{"id": "H1", "action": "evidence|falsify|promote|expire",
                         "evidence_for_delta": 0, "evidence_against_delta": 0, "note": "..."}],
 "new_hypotheses": [{"statement": "...", "prediction": "<testable>",
                     "falsifier": "<what kills it>", "expiry": "YYYY-MM-DD"}],
 "principle_changes": [{"id": "P4", "action": "strengthen|weaken|amend|retire",
                        "new_statement": null, "note": "<cites ledger evidence>"}],
 "promotions": [{"hypothesis_id": "H1", "statement": "...",
                 "type": "entry|exit|sizing|instrument|process|self",
                 "rigidity": "hard|heuristic", "scope": "..."}],
 "fast_track_principles": [{"statement": "...", "type": "...", "rigidity": "...",
                            "scope": "...", "justification": "<why this cannot wait for a hypothesis>"}],
 "counterargument": "<your strongest argument AGAINST your most significant change above>",
 "journal_title": "<one line>",
 "journal_body": "<markdown: ## Decisions reviewed (verdict table) / ## Hypotheses / ## Principle changes / ## Counterargument>"
}"""

RULES = """Reflection rules (binding):
- Judge DECISION QUALITY strictly against what was knowable at decision time; outcomes judge themselves. A good decision can lose money; a lucky bad one must still be called bad.
- Respect sample sizes: do not strengthen/weaken a principle on 1-2 observations unless the evidence is qualitative and structural. Prefer recording evidence over changing rules.
- NEW principles may ONLY come from promoting a hypothesis that met its prediction (promotions[]), or — rarely — a catastrophic lesson via fast_track_principles[] with explicit justification.
- Falsify hypotheses that hit their falsifier; expire lapsed ones; otherwise update evidence.
- The counterargument is mandatory and must genuinely attack your most significant proposed change.
- Change nothing if nothing earned changing — an empty change-set with honest verdicts is a valid reflection."""


def realized_trades(conn, agent_id, since):
    sells = conn.execute(
        "select * from fills where agent_id=%s and side='sell' and ts>%s order by ts",
        (agent_id, since),
    ).fetchall()
    out = []
    for s in sells:
        buys = conn.execute(
            "select qty, fill_price from fills where agent_id=%s and symbol=%s and side='buy' and ts<=%s",
            (agent_id, s["symbol"], s["ts"]),
        ).fetchall()
        tq = sum(float(b["qty"]) for b in buys)
        avg = sum(float(b["qty"]) * float(b["fill_price"]) for b in buys) / tq if tq else None
        out.append({
            "symbol": s["symbol"], "ts": str(s["ts"]), "qty": float(s["qty"]),
            "sold_at": float(s["fill_price"]), "avg_entry": avg,
            "realized_pct": (float(s["fill_price"]) / avg - 1) * 100 if avg else None,
        })
    return out


def last_reflection_ts(conn, agent_id):
    r = conn.execute(
        """select max(started) t from runs
           where agent_id=%s and trigger like 'reflection%%' and status='completed'""",
        (agent_id,),
    ).fetchone()
    if r["t"]:
        return r["t"]
    r = conn.execute("select launched from agent_state where agent_id=%s", (agent_id,)).fetchone()
    return r["launched"] or datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_reflection_prompt(conn, agent_id):
    since = last_reflection_ts(conn, agent_id)
    d = context.TRADER_REPO / "agents" / agent_id
    marks = conn.execute(
        "select ts, equity, bench_index from equity_marks where agent_id=%s and ts>%s order by ts",
        (agent_id, since),
    ).fetchall()
    eq_line = "no marks yet"
    if marks:
        eqs = [float(m["equity"]) for m in marks]
        eq_line = (f"start {eqs[0]:,.0f} → now {eqs[-1]:,.0f} "
                   f"(min {min(eqs):,.0f}, max {max(eqs):,.0f}); "
                   f"bench index now {marks[-1]['bench_index']}")
    closed = realized_trades(conn, agent_id, since)
    pf, _ = context.portfolio_block(conn, agent_id)
    journals = context.recent_journal(agent_id, n=8)
    ops_hist = conn.execute(
        """select o.type, o.verdict, o.reason, o.payload from operations o
           join runs r on r.id=o.run_id
           where r.agent_id=%s and o.created_at>%s order by o.created_at""",
        (agent_id, since),
    ).fetchall()
    rejected = [f"- {o['type']}: {o['reason']} — {json.dumps(o['payload'])[:120]}"
                for o in ops_hist if o["verdict"] == "rejected"]
    return (
        f"{context.read(d / 'harness.md')}\n\n{context.read(d / 'principles.md')}\n\n"
        f"{context.read(d / 'hypotheses.md')}\n\n"
        f"# REFLECTION — {datetime.now(timezone.utc):%Y-%m-%d}\n\n"
        f"This is your scheduled deep self-examination (period since {since}).\n\n"
        f"{RULES}\n\n"
        f"## Equity trajectory this period\n{eq_line}\n\n"
        f"## Closed trades this period (realized)\n"
        f"{json.dumps(closed, indent=1) if closed else 'none'}\n\n"
        f"## Current book\n{pf}\n\n"
        f"## Operations the engine REJECTED this period (constitution violations)\n"
        f"{chr(10).join(rejected) or 'none'}\n\n"
        f"## Your journal this period\n{journals}\n\n"
        f"Respond with ONLY a json object matching exactly this schema:\n{SCHEMA}"
    )


def call_pro(prompt):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                 "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"response_mime_type": "application/json",
                                 "maxOutputTokens": 16384},
        },
        timeout=600,
    )
    r.raise_for_status()
    d = r.json()
    usage = d.get("usageMetadata", {})
    text = d["candidates"][0]["content"]["parts"][0]["text"]
    tin = usage.get("promptTokenCount", 0)
    tout = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return json.loads(text), tin, tout, round(tin * RATE_IN + tout * RATE_OUT, 4)


def apply_reflection(conn, agent_id, run_id, R):
    """Apply the reflection's changes to prose files + record ops. Returns summary lines."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = context.TRADER_REPO / "agents" / agent_id
    P = prose.SectionFile(d / "principles.md")
    H = prose.SectionFile(d / "hypotheses.md")
    seq, summary = 0, []

    def record(optype, payload, verdict="accepted", reason=None):
        nonlocal seq
        seq += 1
        conn.execute(
            """insert into operations (run_id, seq, type, payload, verdict, reason)
               values (%s,%s,%s,%s,%s,%s)""",
            (run_id, seq, optype, json.dumps(payload), verdict, reason),
        )

    for u in R.get("hypothesis_updates", []):
        hid, action = u.get("id"), u.get("action")
        try:
            if action == "evidence":
                H.add_evidence(hid, int(u.get("evidence_for_delta", 0)), int(u.get("evidence_against_delta", 0)))
                H.append_log(hid, date, u.get("note", "evidence updated"))
            elif action in ("falsify", "expire"):
                H.set_status(hid, "falsified" if action == "falsify" else "expired")
                H.append_log(hid, date, u.get("note", action))
                summary.append(f"{hid} {action}d")
            elif action == "promote":
                H.set_status(hid, "promoted")
                H.append_log(hid, date, u.get("note", "promoted to principle"))
            record("hypothesis_op", u)
        except Exception as e:
            record("hypothesis_op", u, "rejected", str(e))

    for p in R.get("promotions", []):
        hid = p.get("hypothesis_id")
        try:
            pid = P.next_id("P")
            P.append_section(prose.new_principle_section(
                pid, p["statement"], p.get("type", "entry"), p.get("rigidity", "heuristic"),
                p.get("scope", ""), f"promoted hypothesis ({hid})", date,
                f"Promoted from {hid} at reflection."))
            summary.append(f"{hid} promoted → {pid}")
            record("principle_op", {"action": "promote", "from": hid, "new_id": pid, **p})
        except Exception as e:
            record("principle_op", p, "rejected", str(e))

    for c in R.get("principle_changes", []):
        pid, action = c.get("id"), c.get("action")
        try:
            if action == "amend" and c.get("new_statement"):
                old = P.statement(pid)
                P.amend_statement(pid, c["new_statement"])
                P.append_log(pid, date, f"Amended (was: “{old}”) — {c.get('note', '')}")
            elif action in ("strengthen", "weaken", "retire"):
                P.set_status(pid, {"strengthen": "strengthened", "weaken": "weakened", "retire": "retired"}[action])
                P.append_log(pid, date, c.get("note", action))
            summary.append(f"{pid} {action}")
            record("principle_op", c)
        except Exception as e:
            record("principle_op", c, "rejected", str(e))

    for f in R.get("fast_track_principles", []):
        try:
            pid = P.next_id("P")
            P.append_section(prose.new_principle_section(
                pid, f["statement"], f.get("type", "process"), f.get("rigidity", "heuristic"),
                f.get("scope", ""), "fast-tracked reflection", date,
                f"FAST-TRACK: {f.get('justification', '')}"))
            summary.append(f"fast-track → {pid}")
            record("principle_op", {"action": "fast_track", "new_id": pid, **f})
        except Exception as e:
            record("principle_op", f, "rejected", str(e))

    for h in R.get("new_hypotheses", []):
        try:
            hid = H.next_id("H")
            H.append_section(prose.new_hypothesis_section(
                hid, h["statement"], h.get("prediction", ""), h.get("falsifier", ""),
                h.get("expiry", ""), date))
            summary.append(f"new hypothesis {hid}")
            record("hypothesis_op", {"action": "propose", "new_id": hid, **h})
        except Exception as e:
            record("hypothesis_op", h, "rejected", str(e))

    P.save()
    H.save()
    conn.commit()
    return summary
