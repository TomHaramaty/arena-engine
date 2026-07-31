"""Build the public data payload from DB state + git prose → site/arena.json.

The workflows push arena.json to the arena-web repo, whose deploy renders
conviction-league.com from it. Nothing else is published.

Usage: python -m jobs.site   (needs DATABASE_URL, TRADER_REPO)
"""
import glob
import json
import os
import pathlib
import re
from datetime import datetime, timezone

from engine import db
from engine import observability as obs
from engine import seating

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))
INITIAL = 100000.0

# The house agents predate the interview picker, so their avatars are assigned
# here — a distinct Member each, matching avatar.js's kit. Seated (interview-born)
# agents carry their own avatar in agents.config; this default only covers house.
META = {
    "tempo":    {"cadence": "Twice daily", "universe": "US large-caps + sector ETFs",   "color": "var(--s2)",
                 "avatar": {"base": "hawk", "color": 4, "costume": "gilet", "acc": "aviators"}},
    "catalyst": {"cadence": "Twice daily", "universe": "US stocks w/ datable catalyst", "color": "var(--s1)",
                 "avatar": {"base": "fox", "color": 0, "costume": "suit", "acc": "none"}},
    "vertex":   {"cadence": "Twice daily", "universe": "Secular-growth large/mid caps", "color": "var(--s3)",
                 "avatar": {"base": "owl", "color": 3, "costume": "professor", "acc": "rounds"}},
    "maverick": {"cadence": "Twice daily", "universe": "Quality names ≥20% off highs",  "color": "var(--s5)",
                 "avatar": {"base": "bull", "color": 1, "costume": "pit", "acc": "visor"}},
    "wildcat":  {"cadence": "Twice daily + self-chosen", "universe": "Equities/ETFs + BTC/ETH", "color": "var(--s4)",
                 "avatar": {"base": "shark", "color": 2, "costume": "hoodie", "acc": "headset"}},
}
# Avatar body hexes — PALS[i][0] in avatar.js. A seated agent's chart line uses
# its own colour so line and portrait agree; house agents keep their var(--sN).
AVATAR_PALETTE = ["#e0684b", "#d19a3f", "#3f9a8f", "#8b6fc9",
                  "#5b7fc0", "#d67aa8", "#7a9a3f", "#7f8a99"]
DEFAULT_AVATAR = {"base": "fox", "color": 0, "costume": "suit", "acc": "none"}
# Seated agents chartered before the avatar picker shipped — their config holds
# no avatar, so their face is assigned here once. ballast keeps its Violet
# tincture (palette index 3). New seated agents never need an entry.
AVATAR_BACKFILL = {
    "ballast": {"base": "bear", "color": 3, "costume": "banker", "acc": "monocle"},
}
BENCH_LABEL = {("SPY",): "SPY", ("QQQ",): "QQQ", ("SPY", "BTC-USD"): "50/50 SPY·BTC"}


def read(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def harness_universe(path):
    """Universe chip for seated agents: the harness constitution's own
    universe line, up to the em-dash or sentence break that starts the
    watchlist boilerplate (deduped harnesses use a plain sentence)."""
    m = re.search(r"(?m)^- Universe:\s*(.+?)\s*(?:—|\.(?:\s|$)|$)", read(path))
    return m.group(1).strip().rstrip(".") if m else ""


def _ev(line):
    m = re.search(r"(\d+)\s*for\s*·?\s*(\d+)\s*against", line)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _md_sections(text):
    """`## Heading` → body, keyed by the heading's first word-group lowercased
    ("Constitution (hard limits — …)" → "constitution")."""
    out = {}
    for m in re.finditer(r"(?ms)^##\s+(.+?)\s*\n(.*?)(?=^##\s+|\Z)", text):
        out[m.group(1).strip().split(" (")[0].strip().lower()] = m.group(2).strip()
    return out


def _bullets(body):
    """`- ` items, with continuation lines folded into the item above."""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
        elif s and out:
            out[-1] += " " + s
    return out


def parse_charter(path, archetype=""):
    """The harness as structured data: what the trader is, what binds it, and
    what has been amended since. Every field is already public in substance —
    the constitution is what the engine enforces on the agent's behalf — and
    the desk needs it verbatim to speak in character without a private fetch."""
    text = read(path)
    if not text:
        return None
    secs = _md_sections(text)
    ident, voice = secs.get("identity", ""), ""
    m = re.search(r"(?s)\bVoice:\s*(.+)$", ident)
    if m:
        voice = m.group(1).strip()
        ident = ident[:m.start()].strip()
    # the archetype is the identity's opening phrase; the credo is what follows
    credo = ident
    if archetype and credo.lower().startswith(archetype.lower()):
        credo = credo[len(archetype):].lstrip(" .—-").strip()
    amendments = []
    for b in _bullets(secs.get("amendments", "")):
        m2 = re.match(r"\*\*(\d{4}-\d{2}-\d{2})\s*[—-]+\s*(.+?)\*\*[.\s]*(.*)$", b, re.S)
        amendments.append({"date": m2.group(1), "title": m2.group(2).strip(),
                           "text": m2.group(3).strip()} if m2
                          else {"date": "", "title": "", "text": b})
    return {
        "credo": credo,
        "voice": voice,
        "mandate": secs.get("mandate", ""),
        "constitution": _bullets(secs.get("constitution", "")),
        "parameters": _bullets(secs.get("parameters", "")),
        "amendments": amendments,
    }


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


def order_trigger(kind, params):
    """The price a resting order fires at, computed the way engine/core.py
    computes it — so the floor can print how far a stop is from being hit
    without ever guessing at the rule behind it."""
    p = params or {}
    try:
        if kind == "trailing_stop":
            return round(float(p["high_water"]) * (1 - float(p["trail_pct"])), 4)
        if kind == "stop":
            return float(p["trigger_price"])
        if kind == "limit":
            return float(p["limit_price"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def arena_curve(conn):
    """Equal-weight arena index across all agents, chain-linked so agents that
    join mid-history enter at the index level of their first mark instead of
    distorting it: each interval the index moves by the mean per-agent return
    of every agent marked at both ends. Points carry epoch `t` for the floor's
    range controls."""
    rows = conn.execute(
        "select ts, agent_id, equity from equity_marks order by ts"
    ).fetchall()
    by_ts = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[r["agent_id"]] = float(r["equity"])
    idx, prev, out = 100.0, {}, []
    for ts in sorted(by_ts):
        cur = by_ts[ts]
        rets = [cur[a] / prev[a] - 1 for a in cur if a in prev and prev[a]]
        if rets:
            idx *= 1 + sum(rets) / len(rets)
        prev.update(cur)
        out.append({"t": int(ts.timestamp()), "date": tlabel(ts), "v": round(idx, 4)})
    return out


AVATAR_PUBLIC_KEYS = ("base", "color", "costume", "acc")


def public_avatar(avatar):
    """The four values the floor needs to draw a face, and nothing else.

    config["avatar"] also carries `chosen` — whether the principal touched the
    seat's picker or kept the random face it offered. That is provenance for
    us; it is not a fact about the trader worth printing on the floor, and
    arena.json is served publicly. Projecting explicitly also stops any future
    config key from reaching a public file by accident.
    """
    avatar = avatar if isinstance(avatar, dict) else {}
    return {k: avatar[k] for k in AVATAR_PUBLIC_KEYS if k in avatar}


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
    # the desk's public half: what the principal filed and what it was answered
    guidance = conn.execute(
        """select cid, text, author, filed_at, disposition, answer, answered_at
           from guidance where agent_id=%s order by id""", (aid,)
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

    curve = [{"t": int(m["ts"].timestamp()), "date": tlabel(m["ts"]),
              "v": round(float(m["equity"]) / INITIAL * 100, 4)} for m in marks]
    bench_curve = [{"t": int(m["ts"].timestamp()), "date": tlabel(m["ts"]),
                    "v": float(m["bench_index"])} for m in marks if m["bench_index"] is not None]
    bench_syms = tuple(st["bench"]["symbols"])
    bidx = bench_curve[-1]["v"] if bench_curve else 100.0
    ret = equity / INITIAL - 1

    d = TRADER / "agents" / aid
    principles = parse_principles(d / "principles.md")
    hyps = parse_hypotheses(d / "hypotheses.md")
    journal = parse_journal(d)
    meta = META.get(aid, {})

    # the four avatar values ride on agents.config for seated agents; house
    # agents get theirs from META. A seated agent's chart line takes its own
    # avatar colour so line and portrait agree.
    cfg = row["config"] if isinstance(row["config"], dict) else {}
    avatar = cfg.get("avatar") if isinstance(cfg.get("avatar"), dict) else None
    avatar = public_avatar(avatar or meta.get("avatar") or AVATAR_BACKFILL.get(aid) or DEFAULT_AVATAR)
    color = meta.get("color") or AVATAR_PALETTE[avatar["color"] % len(AVATAR_PALETTE)]

    # Who chartered it. A principal is named on the floor only if they asked to
    # be (jobs/credit carries that choice into config); silence means anonymous.
    # House agents show no byline at all (operator ruling 2026-07-30) — the same
    # silence as a principal who declined credit, asserting nothing either way.
    credit = cfg.get("credit") if isinstance(cfg.get("credit"), dict) else {}
    chartered_by = (str(credit.get("name") or "") if credit.get("show") else "") \
        if row["owner_uid"] else ""

    return {
        # `brain` is deliberately absent: which model runs a trader is an
        # internal codename, not a product fact, and the floor never showed it.
        "id": aid, "name": row["name"], "archetype": row["archetype"],
        "chartered_by": chartered_by,
        "cadence": meta.get("cadence", "Twice daily"),
        "universe": meta.get("universe") or harness_universe(d / "harness.md"),
        "color": color, "avatar": avatar,
        "launched": str(st["launched"] or ""),
        "benchmark_label": BENCH_LABEL.get(bench_syms, "/".join(bench_syms)),
        "equity": equity, "cash": cash, "cash_pct": cash / equity if equity else 0,
        "ret": ret, "alpha": ret - (bidx / 100 - 1),
        "max_dd": max_drawdown([float(m["equity"]) for m in marks] or [equity]),
        "curve": curve or [{
            # launched is a date; a newborn's lone point sits at its launch day
            **({"t": int(datetime.combine(st["launched"], datetime.min.time(),
                                          tzinfo=timezone.utc).timestamp()),
                "date": st["launched"].strftime("%b %d")} if st["launched"] else {"date": ""}),
            "v": round(equity / INITIAL * 100, 4)}],
        "bench_curve": bench_curve,
        "positions": pos_out,
        # Resting rules, with the price each one fires at and the price it is
        # watching — so the floor can say how close a stop is to being hit
        # instead of printing the raw params blob it used to.
        "standing_orders": [
            {"kind": o["kind"], "side": o["side"], "symbol": o["symbol"],
             "qty": float(o["qty"]) if o["qty"] is not None else None,
             "trigger": order_trigger(o["kind"], o["params"]),
             "mark": prices.get(o["symbol"]),
             "placed": tlabel(o["created_at"]),
             "note": o["reason"] or ""} for o in standing
        ],
        "fills": [
            {"ts": tlabel(f["ts"]), "symbol": f["symbol"], "side": f["side"],
             "qty": float(f["qty"]), "fill_price": float(f["fill_price"])} for f in fills
        ],
        "charter": parse_charter(d / "harness.md", row["archetype"] or ""),
        "guidance": [
            {"cid": g["cid"], "text": g["text"], "author": g["author"],
             "filed": g["filed_at"].strftime("%Y-%m-%d"),
             "disposition": g["disposition"] or "",
             "answer": g["answer"] or "",
             "answered": g["answered_at"].strftime("%Y-%m-%d") if g["answered_at"] else ""}
            for g in guidance
        ],
        "journal": journal, "principles": principles, "hypotheses": hyps,
        "n_principles": sum(1 for p in principles if p["status"] != "retired"),
        "n_revisions": sum(max(0, len(p["changelog"]) - 1) for p in principles),
        "n_hyp_testing": sum(1 for h in hyps if h["status"] == "testing"),
        "last_action": journal[0]["title"] if journal else "—",
    }


def system_block(conn):
    """What the public artifact says about the machine: only how fresh the
    prices are. Run costs, token counts and the operations ledger are the
    operator's — `python3 -m jobs.ops` reads them from Postgres. Publishing a
    running bill on the floor told a visitor nothing and anchored a price on a
    product that has not set one."""
    last = conn.execute("select max(ts) t from ticks").fetchone()
    symbols = conn.execute("select count(distinct symbol) n from ticks").fetchone()["n"]
    return {
        "last_update": tlabel(last["t"]) if last["t"] else "never",
        "symbols_tracked": symbols,
    }


def tape_block(conn, limit=150):
    """Every action the floor took, newest first, built only from what the
    engine wrote at the time.

    A trade's words come from wherever the record actually holds them: a market
    buy is given the thesis from the accepted operation of its own run (the
    orders table has no reason column for market orders), a stop or limit
    carries the note the trader armed it with, and a market sell gets none —
    the op contract never asked for one. Nothing is composed after the fact and
    nothing is written back into orders to make this tidier.

    A refused trade is on the tape too: a trader reaching past its constitution
    and being stopped in code is the floor's whole claim, made watchable.
    Refused standing orders are not — they are nearly always the cascade of a
    refused buy (no position left to protect) and read as noise."""
    ev = []

    theses = {}
    for r in conn.execute(
        """select r.id run_id, o.payload from operations o join runs r on r.id=o.run_id
           where o.type='place_order' and o.verdict='accepted'"""
    ).fetchall():
        p = r["payload"] or {}
        if p.get("thesis"):
            theses[(r["run_id"], p.get("symbol"))] = p["thesis"]

    for f in conn.execute(
        """select f.*, o.kind, o.reason, o.run_id from fills f
           left join orders o on o.id = f.order_id
           order by f.ts desc limit %s""", (limit,)
    ).fetchall():
        ev.append({
            "t": int(f["ts"].timestamp()), "when": tlabel(f["ts"]),
            "agent": f["agent_id"], "event": "fill", "side": f["side"],
            "symbol": f["symbol"], "qty": float(f["qty"]),
            "price": float(f["fill_price"]),
            "mechanism": f["kind"] or "market",
            "note": f["reason"] or theses.get((f["run_id"], f["symbol"]), ""),
        })

    for o in conn.execute(
        """select * from orders where kind <> 'market'
           order by created_at desc limit %s""", (limit,)
    ).fetchall():
        base = {"agent": o["agent_id"], "side": o["side"], "symbol": o["symbol"],
                "mechanism": o["kind"], "trigger": order_trigger(o["kind"], o["params"]),
                "note": o["reason"] or ""}
        ev.append({"t": int(o["created_at"].timestamp()),
                   "when": tlabel(o["created_at"]), "event": "armed", **base})
        if o["status"] == "canceled" and o["closed_at"]:
            ev.append({"t": int(o["closed_at"].timestamp()),
                       "when": tlabel(o["closed_at"]), "event": "pulled", **base})

    for o in conn.execute(
        """select o.*, r.agent_id from operations o join runs r on r.id=o.run_id
           where o.verdict='rejected' and o.type='place_order'
           order by o.created_at desc limit %s""", (limit,)
    ).fetchall():
        p = o["payload"] or {}
        ev.append({
            "t": int(o["created_at"].timestamp()), "when": tlabel(o["created_at"]),
            "agent": o["agent_id"], "event": "blocked", "side": p.get("side", ""),
            "symbol": p.get("symbol", ""),
            "notional": float(p["notional_usd"]) if p.get("notional_usd") else None,
            "note": o["reason"] or "",
        })

    ev.sort(key=lambda e: e["t"], reverse=True)
    return ev[:limit]


def main():
    obs.init("site")
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
        "arena_curve": arena_curve(conn),
        "tape": tape_block(conn),
        "system": system_block(conn),
        # Names the engine will refuse whatever else a packet says: house
        # agents, registry words, and every ticker in the universe. Published
        # so the interview can refuse one the moment it is proposed instead of
        # letting a principal spend fifteen minutes on a charter that seating
        # was always going to reject. The floor's own ids are already in
        # `agents`, so they are deliberately not repeated here.
        "reserved": sorted(seating.RESERVED),
    }
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    (site / "arena.json").write_text(json.dumps(data, indent=1))
    print(f"site built: {len(data['agents'])} agents, {len(data['tape'])} tape events, "
          f"prices {data['system']['last_update']}")


if __name__ == "__main__":
    main()
