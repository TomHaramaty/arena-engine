"""Build the public interface from DB state + git prose → site/ (arena.json + index.html).

Usage: python -m jobs.site   (needs DATABASE_URL, TRADER_REPO)
"""
import glob
import json
import os
import pathlib
import re
from datetime import datetime, timezone

from engine import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))
INITIAL = 100000.0

META = {
    "tempo":    {"cadence": "Daily", "universe": "US large-caps + sector ETFs",   "color": "var(--s2)"},
    "catalyst": {"cadence": "Daily", "universe": "US stocks w/ datable catalyst", "color": "var(--s1)"},
    "vertex":   {"cadence": "Daily", "universe": "Secular-growth large/mid caps", "color": "var(--s3)"},
    "maverick": {"cadence": "Daily", "universe": "Quality names ≥20% off highs",  "color": "var(--s5)"},
    "wildcat":  {"cadence": "Daily + self-chosen", "universe": "Equities/ETFs + BTC/ETH", "color": "var(--s4)"},
}
BENCH_LABEL = {("SPY",): "SPY", ("QQQ",): "QQQ", ("SPY", "BTC-USD"): "50/50 SPY·BTC"}


def read(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _ev(line):
    m = re.search(r"(\d+)\s*for\s*·?\s*(\d+)\s*against", line)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def parse_principles(path):
    out, cur, mode = [], None, None
    for raw in read(path).splitlines():
        line = raw.rstrip()
        h = re.match(r"^##\s+(P\d+)\s*·\s*(.+)$", line)
        if h:
            if cur:
                out.append(cur)
            cur = {"id": h.group(1), "statement": h.group(2).strip(), "detail": "",
                   "type": "", "rigidity": "", "scope": "", "origin": "", "status": "active",
                   "ev_for": 0, "ev_against": 0, "changelog": []}
            mode = None
            continue
        if cur is None:
            continue
        s = line.strip()
        if s and not s.startswith("- ") and mode is None:
            # free-form elaboration between the heading and the metadata lines —
            # principles may be as long as the agent needs
            cur["detail"] = (cur["detail"] + "\n" + s).strip()
        elif s.startswith("- type:") or s.startswith("- origin:"):
            for part in s[1:].split("·"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    if k.strip() in ("type", "rigidity", "scope", "origin", "status"):
                        cur[k.strip()] = v.strip()
        elif s.startswith("- evidence:"):
            cur["ev_for"], cur["ev_against"] = _ev(s)
        elif s.startswith("- changelog"):
            mode = "log"
        elif mode == "log" and s.startswith("- "):
            m = re.match(r"-\s*(\d{4}-\d{2}-\d{2}):\s*(.+)$", s)
            if m:
                cur["changelog"].append({"date": m.group(1), "text": m.group(2).strip()})
    if cur:
        out.append(cur)
    return out


def parse_hypotheses(path):
    out, cur = [], None
    for raw in read(path).splitlines():
        s = raw.strip()
        h = re.match(r"^##\s+(H\d+)\s*·\s*(.+)$", s)
        if h:
            if cur:
                out.append(cur)
            cur = {"id": h.group(1), "statement": h.group(2).strip(), "status": "testing",
                   "prediction": "", "falsifier": "", "expiry": "", "ev_for": 0,
                   "ev_against": 0, "note": ""}
            continue
        if cur is None:
            continue
        for key in ("status", "prediction", "falsifier", "expiry"):
            if s.lower().startswith(f"- {key}:"):
                cur[key] = s.split(":", 1)[1].strip()
        if s.startswith("- evidence:"):
            cur["ev_for"], cur["ev_against"] = _ev(s)
    if cur:
        out.append(cur)
    return out


def parse_journal(agent_dir):
    entries = []
    for path in sorted(glob.glob(str(agent_dir / "journal" / "*.md")), reverse=True):
        text = pathlib.Path(path).read_text(encoding="utf-8")
        for b in re.split(r"(?m)^#\s+(?=\d{4}-\d{2}-\d{2}\s*[—-])", text):
            b = b.strip()
            if not b:
                continue
            b = re.split(r"(?m)^---\s*$", b, maxsplit=1)[0].strip()
            head = b.splitlines()[0]
            # collapse an accidentally doubled leading date ("2026-07-23 — 2026-07-23 — …")
            head = re.sub(r"^(\d{4}-\d{2}-\d{2})(\s*[—-]+\s*\d{4}-\d{2}-\d{2})+", r"\1", head)
            m = re.match(r"(\d{4}-\d{2}-\d{2})\s*[—-]+\s*(\w+)\s*[—-]+\s*(.+)", head)
            if not m:
                m2 = re.match(r"(\d{4}-\d{2}-\d{2})\s*[—-]+\s*(.+)", head)
                if not m2:
                    continue
                date, typ, title = m2.group(1), "HOLD", m2.group(2).strip()
            else:
                date, typ, title = m.group(1), m.group(2).upper(), m.group(3).strip()
            secs = {}
            for sm in re.finditer(r"(?ms)^##\s+(.+?)\s*\n(.*?)(?=^##\s+|\Z)", b):
                secs[sm.group(1).strip().lower()] = sm.group(2).strip()
            entries.append({
                "date": date,
                "type": {"TRADE": "trade", "HOLD": "hold", "REFLECTION": "reflect"}.get(typ, "hold"),
                "title": title,
                "rationale": secs.get("rationale", ""),
                "actions": secs.get("actions", ""),
            })
    return entries


def max_drawdown(vals):
    peak, dd = INITIAL, 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


def tlabel(ts):
    return ts.strftime("%b %d %H:%M")


def build_agent(conn, row, prices):
    aid = row["id"]
    st = conn.execute("select * from agent_state where agent_id=%s", (aid,)).fetchone()
    marks = conn.execute(
        "select ts, equity, bench_index from equity_marks where agent_id=%s order by ts",
        (aid,),
    ).fetchall()
    pos = conn.execute(
        "select * from positions where agent_id=%s order by symbol", (aid,)
    ).fetchall()
    fills = conn.execute(
        "select * from fills where agent_id=%s order by ts desc limit 30", (aid,)
    ).fetchall()
    standing = conn.execute(
        "select * from orders where agent_id=%s and status='open' order by id", (aid,)
    ).fetchall()

    cash = float(st["cash"])
    pos_out, pv = [], 0.0
    for p in pos:
        mark = prices.get(p["symbol"], float(p["avg_fill"]))
        val = float(p["qty"]) * mark
        pv += val
        pos_out.append({
            "symbol": p["symbol"], "qty": float(p["qty"]),
            "fill_price": float(p["avg_fill"]), "mark": mark, "value": val,
            "weight": 0.0, "pl": mark / float(p["avg_fill"]) - 1,
            "thesis": p["thesis"] or "", "review_by": str(p["review_by"] or ""),
        })
    equity = cash + pv
    for p in pos_out:
        p["weight"] = p["value"] / equity if equity else 0

    curve = [{"date": tlabel(m["ts"]), "v": round(float(m["equity"]) / INITIAL * 100, 4)} for m in marks]
    bench_curve = [{"date": tlabel(m["ts"]), "v": float(m["bench_index"])} for m in marks if m["bench_index"] is not None]
    bench_syms = tuple(st["bench"]["symbols"])
    bidx = bench_curve[-1]["v"] if bench_curve else 100.0
    ret = equity / INITIAL - 1

    d = TRADER / "agents" / aid
    principles = parse_principles(d / "principles.md")
    hyps = parse_hypotheses(d / "hypotheses.md")
    journal = parse_journal(d)
    meta = META.get(aid, {})

    return {
        "id": aid, "name": row["name"], "archetype": row["archetype"],
        "brain": row["brain"],
        "cadence": meta.get("cadence", "Daily"), "universe": meta.get("universe", ""),
        "color": meta.get("color", "var(--s1)"),
        "launched": str(st["launched"] or ""),
        "benchmark_label": BENCH_LABEL.get(bench_syms, "/".join(bench_syms)),
        "equity": equity, "cash": cash, "cash_pct": cash / equity if equity else 0,
        "ret": ret, "alpha": ret - (bidx / 100 - 1),
        "max_dd": max_drawdown([float(m["equity"]) for m in marks] or [equity]),
        "curve": curve or [{"date": str(st["launched"] or ""), "v": round(equity / INITIAL * 100, 4)}],
        "bench_curve": bench_curve,
        "positions": pos_out,
        "standing_orders": [
            {"id": o["id"], "kind": o["kind"], "side": o["side"], "symbol": o["symbol"],
             "qty": float(o["qty"]) if o["qty"] is not None else None,
             "params": o["params"], "note": o["reason"] or ""} for o in standing
        ],
        "fills": [
            {"ts": tlabel(f["ts"]), "symbol": f["symbol"], "side": f["side"],
             "qty": float(f["qty"]), "fill_price": float(f["fill_price"])} for f in fills
        ],
        "journal": journal, "principles": principles, "hypotheses": hyps,
        "n_principles": sum(1 for p in principles if p["status"] != "retired"),
        "n_revisions": sum(max(0, len(p["changelog"]) - 1) for p in principles),
        "n_hyp_testing": sum(1 for h in hyps if h["status"] == "testing"),
        "last_action": journal[0]["title"] if journal else "—",
    }


def system_block(conn):
    last_tick = conn.execute("select max(ts) t, count(distinct symbol) n from ticks where ts = (select max(ts) from ticks)").fetchone()
    tick_count = conn.execute("select count(distinct symbol) n from ticks").fetchone()["n"]
    runs = conn.execute(
        """select r.*, a.name from runs r join agents a on a.id=r.agent_id
           order by r.started desc limit 25"""
    ).fetchall()
    total_cost = conn.execute("select coalesce(sum(cost_usd),0) c from runs").fetchone()["c"]
    ops = conn.execute(
        """select o.*, r.agent_id from operations o join runs r on r.id=o.run_id
           order by o.created_at desc limit 40"""
    ).fetchall()
    trig = conn.execute(
        "select * from triggers_fired order by ts desc limit 20"
    ).fetchall()
    return {
        "last_tick": tlabel(last_tick["t"]) if last_tick["t"] else "never",
        "symbols_tracked": tick_count,
        "total_cost_usd": float(total_cost),
        "runs": [
            {"id": r["id"], "agent": r["agent_id"], "trigger": r["trigger"],
             "status": r["status"], "started": tlabel(r["started"]),
             "cost": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
             "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"]} for r in runs
        ],
        "ops": [
            {"agent": o["agent_id"], "type": o["type"], "verdict": o["verdict"],
             "reason": o["reason"] or "", "ts": tlabel(o["created_at"]),
             "summary": json.dumps(o["payload"])[:140]} for o in ops
        ],
        "triggers": [
            {"agent": t["agent_id"], "kind": t["kind"], "ts": tlabel(t["ts"]),
             "handled": t["handled"], "details": json.dumps(t["details"])[:120]} for t in trig
        ],
    }


def main():
    conn = db.connect()
    agents_rows = conn.execute(
        "select * from agents where status='active' order by id"
    ).fetchall()
    prices = {
        r["symbol"]: float(r["price"])
        for r in conn.execute(
            "select distinct on (symbol) symbol, price from ticks order by symbol, ts desc"
        ).fetchall()
    }
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "initial_capital": INITIAL,
        "agents": [build_agent(conn, r, prices) for r in agents_rows],
        "system": system_block(conn),
    }
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    (site / "arena.json").write_text(json.dumps(data, indent=1))
    template = (ROOT / "web" / "template.html").read_text()
    (site / "index.html").write_text(template.replace("/*__ARENA_DATA__*/", json.dumps(data)))
    print(f"site built: {len(data['agents'])} agents, {len(data['system']['runs'])} runs, "
          f"total spend ${data['system']['total_cost_usd']:.2f}")


if __name__ == "__main__":
    main()
