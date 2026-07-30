"""Read the arena its own rights: a standing check on the book and the record.

Every defect found in the 2026-07-30 robustness audit had been live for days and
was invisible until somebody went looking with a SQL prompt:

  · two agents woke 13 times a night on a drawdown they had already argued
    about, spending brain runs and appending journal entries that cannot be
    removed;
  · seven runs sat in 'started' forever, indistinguishable from runs in flight;
  · a real principal's trader went onto the floor with a 404 for a face.

None of it fired an alert, because nothing in the system ever asked "does the
record still add up?" This does, on every tick.

The checks are ordinary arithmetic and ordinary counting, and they are separated
from the queries that feed them so they can be tested without a database. The
strongest one is `book_against_fills`: cash and positions must equal the sum of
every fill ever printed. Nothing in the engine is allowed to move the book
without printing one, so any drift means a write went somewhere it should not
have — the one class of bug that would make the whole record untrustworthy.

  python -m jobs.doctor              # report; exit 1 on anything at ERROR
  python -m jobs.doctor --quiet      # findings only
  python -m jobs.doctor --no-network # skip the published-face check
"""
import os
import sys
from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone

from engine import db
from engine import observability as obs

Finding = namedtuple("Finding", "level check subject detail")

ERROR, WARN = "ERROR", "WARN"

STARTING_CASH = 100_000.0
# A session that has not finished in this long is not running, it has died.
# The longest real run on the record took ~11 minutes.
STUCK_AFTER = timedelta(minutes=90)
# One wake per agent per kind, per day, is normal. Several is the signature of a
# condition re-firing on facts the agent has already seen — the drawdown loop,
# and whatever the next one turns out to be.
WAKES_PER_DAY_LIMIT = 4
# A trigger nobody has acted on. The tick dispatches these every half hour.
TRIGGER_STALE_AFTER = timedelta(hours=3)
# Marks land on every tick that has quotes. Crypto quotes around the clock, so
# even a weekend should not be quiet for long.
MARK_STALE_AFTER = timedelta(hours=8)
# Cash is dollars; a cent of float drift across thousands of fills is not news.
CENT = 0.01


# ---------- the checks: pure, so they can be tested honestly ----------

def stuck_runs(runs, now):
    """Runs still 'started' long after any real session would have finished."""
    out = []
    for r in runs:
        if r["status"] != "started" or now - r["started"] <= STUCK_AFTER:
            continue
        out.append(Finding(
            WARN, "stuck-run", f"{r['agent_id']} run {r['id']}",
            f"still 'started' {_ago(now - r['started'])} after it began "
            f"({r['trigger']}) — the session died and is never re-fired"))
    return out


def stranded_journals(runs):
    """A journal written to Postgres whose run never completed: the operations
    reached the book and the entry explaining them may never have reached the
    record. These are recoverable — the text is right there."""
    return [
        Finding(ERROR, "stranded-journal", f"{r['agent_id']} run {r['id']}",
                f"the book moved and the entry may not have been filed: "
                f"\"{(r['journal_title'] or '')[:60]}\" is held in runs.meta")
        for r in runs
        if r["status"] != "completed" and r.get("journal_title") is not None
    ]


def wake_loops(triggers, now):
    """The same agent woken over and over by the same kind of condition."""
    day = now - timedelta(days=1)
    counts = Counter(
        (t["agent_id"], t["kind"]) for t in triggers if t["ts"] > day
    )
    return [
        Finding(ERROR, "wake-loop", f"{aid} · {kind}",
                f"{n} {kind} triggers in 24h (limit {WAKES_PER_DAY_LIMIT}) — a "
                f"condition is re-firing on facts already deliberated on; each "
                f"one spends a brain run and appends to the record")
        for (aid, kind), n in sorted(counts.items()) if n > WAKES_PER_DAY_LIMIT
    ]


def launch_baselines(repo):
    """The day-one book, which predates the fills ledger.

    The founding five were seeded on 2026-07-22 from `agents/<id>/portfolio.json`
    — cash and positions written straight into the tables, with no fills printed,
    because there was nothing to print them from. Those snapshots are the
    starting balance the fills are added to. Without them this check reads the
    launch as $80,000 of unexplained cash, which is exactly the kind of standing
    false alarm that teaches an operator to ignore a checker.
    """
    out = {}
    agents_dir = (repo / "agents") if repo else None
    if not agents_dir or not agents_dir.exists():
        return out
    for path in sorted(agents_dir.glob("*/portfolio.json")):
        import json
        try:
            pf = json.loads(path.read_text())
        except ValueError:
            continue
        out[path.parent.name] = {
            "cash": float(pf.get("cash", STARTING_CASH)),
            "pos": Counter({p["symbol"]: float(p["qty"])
                            for p in pf.get("positions") or []}),
        }
    return out


def book_against_fills(states, fills, baselines=None):
    """Cash and positions must equal the launch book plus every fill printed
    since.

    The engine moves cash only alongside a fill, in the same transaction, in
    both places that can move it (runner/ops for market orders, engine/core for
    triggered ones). So this is a double entry: if it does not balance, a write
    escaped the fill it belongs to, and no number the floor publishes can be
    trusted until it is explained.
    """
    baselines = baselines or {}

    def start(aid):
        b = baselines.get(aid)
        return ({"cash": float(b["cash"]), "pos": Counter(b["pos"])} if b
                else {"cash": STARTING_CASH, "pos": Counter()})

    by_agent = {}
    for f in fills:
        b = by_agent.setdefault(f["agent_id"], start(f["agent_id"]))
        signed = float(f["qty"]) * float(f["fill_price"])
        if f["side"] == "buy":
            b["cash"] -= signed
            b["pos"][f["symbol"]] += float(f["qty"])
        else:
            b["cash"] += signed
            b["pos"][f["symbol"]] -= float(f["qty"])

    out = []
    for st in states:
        aid = st["agent_id"]
        b = by_agent.get(aid) or start(aid)
        drift = float(st["cash"]) - b["cash"]
        if abs(drift) > CENT:
            out.append(Finding(
                ERROR, "book-drift", aid,
                f"cash is ${float(st['cash']):,.2f} but the fills add to "
                f"${b['cash']:,.2f} (${drift:+,.2f} unexplained)"))
        held = {s: float(q) for s, q in (st.get("positions") or {}).items()}
        for sym in set(held) | {s for s, q in b["pos"].items() if abs(q) > 1e-6}:
            from_fills, on_book = b["pos"].get(sym, 0.0), held.get(sym, 0.0)
            if abs(on_book - from_fills) > 1e-6:
                out.append(Finding(
                    ERROR, "book-drift", f"{aid} · {sym}",
                    f"position is {on_book:.6f} but the fills add to "
                    f"{from_fills:.6f}"))
    return out


def stale_marks(states, now):
    """A launched agent the arena has stopped marking is an agent whose numbers
    on the floor are quietly frozen."""
    return [
        Finding(WARN, "stale-mark", st["agent_id"],
                f"last marked {_ago(now - st['last_mark'])} ago — the floor is "
                f"showing a frozen equity"
                if st["last_mark"] else
                "launched but never marked — nothing to publish")
        for st in states
        if st.get("launched") and (
            st["last_mark"] is None or now - st["last_mark"] > MARK_STALE_AFTER)
    ]


def stale_triggers(triggers, now):
    """A wake nobody answered: the dispatcher runs every half hour, so an
    unhandled trigger hours old means the dispatch is not reaching it."""
    return [
        Finding(WARN, "unanswered-wake", f"{t['agent_id']} · {t['kind']}",
                f"filed {_ago(now - t['ts'])} ago and still unhandled")
        for t in triggers
        if not t["handled"] and now - t["ts"] > TRIGGER_STALE_AFTER
    ]


def missing_journals(runs, repo):
    """Every completed session left an entry. Check the file is really there —
    the record is the published artifact, and Postgres saying 'completed' is not
    the same fact as a reader being able to read it."""
    if not repo or not repo.exists():
        return []
    out = []
    for r in runs:
        if r["status"] != "completed":
            continue
        day = r["started"].astimezone(timezone.utc).date().isoformat()
        path = repo / "agents" / r["agent_id"] / "journal" / f"{day}.md"
        if not path.exists():
            out.append(Finding(
                ERROR, "missing-entry", f"{r['agent_id']} run {r['id']}",
                f"run completed but agents/{r['agent_id']}/journal/{day}.md is "
                f"not in the record"))
    return out


def missing_faces(agent_ids, head):
    """A seated trader with no rendered face 404s on the floor and puts a broken
    image in its principal's letter. It happened to vector on 2026-07-29,
    because the generator has to be run by hand after a seating."""
    out = []
    for aid in agent_ids:
        url = f"https://conviction-league.com/avatars/{aid}.png"
        try:
            code = head(url)
        except Exception as e:                       # network, not the arena
            return [Finding(WARN, "face-check", "-", f"could not check faces: {e}")]
        if code != 200:
            out.append(Finding(
                WARN, "missing-face", aid,
                f"{url} returns {code} — run arena-web tools/gen-avatars.mjs "
                f"{aid} and commit it"))
    return out


def _ago(delta):
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f}h" if hours >= 1 else f"{delta.total_seconds() / 60:.0f}m"


# ---------- reading the live arena ----------

def gather(conn, repo=None, head=None, now=None):
    now = now or datetime.now(timezone.utc)
    runs = conn.execute(
        """select id, agent_id, trigger, status, started,
                  meta->'journal'->>'title' as journal_title
           from runs where started > now() - interval '7 days'
           order by started"""
    ).fetchall()
    triggers = conn.execute(
        """select agent_id, kind, handled, ts from triggers_fired
           where ts > now() - interval '7 days'"""
    ).fetchall()
    states = conn.execute(
        """select s.agent_id, s.cash, s.launched,
                  (select max(ts) from equity_marks m where m.agent_id=s.agent_id)
                    as last_mark,
                  (select coalesce(jsonb_object_agg(p.symbol, p.qty), '{}'::jsonb)
                     from positions p where p.agent_id=s.agent_id) as positions
           from agent_state s join agents a on a.id=s.agent_id
           where a.status='active'"""
    ).fetchall()
    fills = conn.execute(
        "select agent_id, symbol, side, qty, fill_price from fills"
    ).fetchall()

    findings = (
        stranded_journals(runs)
        + wake_loops(triggers, now)
        + book_against_fills(states, fills, launch_baselines(repo))
        + stuck_runs(runs, now)
        + stale_marks(states, now)
        + stale_triggers(triggers, now)
        + missing_journals(runs, repo)
    )
    if head:
        findings += missing_faces([st["agent_id"] for st in states], head)
    return findings


def _head(url):
    import requests
    return requests.head(url, timeout=15, allow_redirects=True).status_code


def report(findings, quiet=False):
    if not findings:
        if not quiet:
            print("doctor: the book balances, the record is complete, "
                  "nothing is looping.")
        return 0
    order = {ERROR: 0, WARN: 1}
    for f in sorted(findings, key=lambda f: (order[f.level], f.check)):
        print(f"{f.level:5s} {f.check:18s} {f.subject:28s} {f.detail}")
    errors = [f for f in findings if f.level == ERROR]
    print(f"doctor: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 1 if errors else 0


def main():
    obs.init("doctor")
    import pathlib
    repo = os.environ.get("TRADER_REPO")
    findings = gather(
        db.connect(),
        repo=pathlib.Path(repo) if repo else None,
        head=None if "--no-network" in sys.argv else _head,
    )
    code = report(findings, quiet="--quiet" in sys.argv)
    if code and obs.init("doctor"):
        import sentry_sdk
        sentry_sdk.capture_message(
            "doctor: " + "; ".join(
                f"{f.check} {f.subject}" for f in findings if f.level == ERROR),
            level="error")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
