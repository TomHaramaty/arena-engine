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
import contextlib
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
# ...and at least one of them this recently, or the loop is over and the count is
# only history. The tick runs twice an hour, so a live loop always has one.
LOOP_IS_LIVE_WITHIN = timedelta(hours=2)
# A trigger nobody has acted on. The tick dispatches these every half hour.
TRIGGER_STALE_AFTER = timedelta(hours=3)
# Marks land on every tick that has quotes. Crypto quotes around the clock, so
# even a weekend should not be quiet for long.
MARK_STALE_AFTER = timedelta(hours=8)
# Cash is dollars; a cent of float drift across thousands of fills is not news.
CENT = 0.01
# How long an entry is allowed to still be on its way before its absence means
# anything. The record is pushed from four workflows (tick, daily-run, reflect,
# first-bell) out of four separate clones, each retrying a rejected push with a
# rebase, and doctor reads a clone taken at the start of its own job — so "not in
# my copy" and "not in the record" are different facts for a while after a
# session ends. Generous on purpose: a journal that really was lost is still
# reported on the next tick half an hour later, and the whole worth of this check
# is that a finding means something.
JOURNAL_LANDS_WITHIN = timedelta(minutes=15)
# Attempts at one operation type before a 100% refusal rate stops being bad luck
# and starts being a broken promise. Low on purpose: the point is to catch it in
# the first days, not after a dozen agents have reasoned around it.
DEAD_CAPABILITY_FLOOR = 5


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
    """The same agent woken over and over by the same kind of condition.

    Both halves matter: a day's worth of wakes, AND one of them recent. A loop
    that has been fixed keeps its count for another 24 hours, and a checker that
    goes on failing every tick over something already dealt with is how an
    operator learns to ignore it. The question is whether this is happening now.
    """
    day, recent = now - timedelta(days=1), now - LOOP_IS_LIVE_WITHIN
    counts, latest = Counter(), {}
    for t in triggers:
        if t["ts"] <= day:
            continue
        key = (t["agent_id"], t["kind"])
        counts[key] += 1
        latest[key] = max(latest.get(key, t["ts"]), t["ts"])
    return [
        Finding(ERROR, "wake-loop", f"{aid} · {kind}",
                f"{n} {kind} triggers in 24h (limit {WAKES_PER_DAY_LIMIT}), the "
                f"last {_ago(now - latest[(aid, kind)])} ago — a condition is "
                f"re-firing on facts already deliberated on; each one spends a "
                f"brain run and appends to the record")
        for (aid, kind), n in sorted(counts.items())
        if n > WAKES_PER_DAY_LIMIT and latest[(aid, kind)] > recent
    ]


def launch_baselines(repo):
    """The day-one book, which predates the fills ledger.

    The founding five were seeded on 2026-07-22 from `agents/<id>/portfolio.json`
    — cash and positions written straight into the tables, with no fills printed,
    because there was nothing to print them from. Those snapshots are the
    starting balance the fills are added to. Without them this check reads the
    launch as $80,000 of unexplained cash, which is exactly the kind of standing
    false alarm that teaches an operator to ignore a checker.

    None means the record could not be read at all, which is a different fact
    from "no agent has a snapshot" and has to stay tellable apart from it: on
    2026-08-05 this returned {} for a clone that had failed, and the check it
    feeds duly reported $25,000, $25,000 and $80,000 of unexplained cash on
    maverick, tempo and vertex — their launch snapshots, to the dollar. The
    paragraph above had already called that shot; the code just had no way to
    say "I could not look."
    """
    out = {}
    agents_dir = (repo / "agents") if repo else None
    if not agents_dir or not agents_dir.exists():
        return None
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


def check_book(states, fills, baselines):
    """The book check, or an honest refusal to make it.

    book_against_fills is the strongest thing here and the loudest when it
    fires, so it must never be run on inputs it cannot stand behind. Without the
    launch snapshots the founding five's seeded cash and positions have no fills
    to explain them, and the check reports the day the arena opened as money
    gone missing — eight ERRORs on 2026-08-05, every one of them false, from a
    tick whose clone step had failed a minute earlier.

    jobs/site already refuses in exactly this case ("refusing to publish a
    record without its prose"). This is the same refusal for the same reason: a
    check that did not run is a fact worth reporting, and it is not the same
    fact as a check that passed.
    """
    if baselines is None:
        return [Finding(
            ERROR, "book-uncheckable", "-",
            "the record is not here (no agents/ under TRADER_REPO), so the "
            "launch snapshots cannot be read and cash cannot be checked "
            "against the fills — this says nothing about the book, only that "
            "nobody checked it")]
    return book_against_fills(states, fills, baselines)


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


def checkout_time(repo):
    """When this working copy last saw the record. A run that completed after
    that cannot be expected to be in it.

    Without this the check is only honest against a fresh clone, which is how CI
    runs it — but run from a laptop two hours behind, it reports a real
    principal's first session as missing from the record. A checker that is wrong
    when a human runs it by hand is a checker that human stops running.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, check=True).stdout.strip()
        return datetime.fromisoformat(out)
    except Exception:
        return None


def dead_capabilities(op_counts, floor=DEAD_CAPABILITY_FLOOR):
    """An operation the engine has refused every single time it was attempted.

    The arena's contract tells every brain what it may do. When the runtime
    cannot honour one of those promises, nothing announces it: the agent files
    the operation, gets a rejection, reasons around it, and writes the detour
    into an append-only record as though it were a considered choice.

    That is not hypothetical. `watchlist_request` was in the operations
    contract from the beginning, promising that a symbol an agent asked for
    would be resolved, priced and tradable in the same run. FINNHUB_KEY was set
    on one step of one workflow, so no brain run could ever resolve anything,
    and all 12 requests ever filed were refused. Discovered 2026-07-31 by
    reading the operations table by hand, eight days in. This check is what
    would have said so on day one.

    A capability with a handful of attempts and a 100% refusal rate is broken,
    not unlucky. One rejection is an agent being told no; a hundred with no
    acceptance anywhere in the record is the engine failing to keep a promise
    it is still making in the prompt.

    op_counts: rows of {type, verdict, n} over the whole record, not a window —
    a capability that worked once in April and has failed ever since is a
    different finding, and this one deliberately never fires on it.
    """
    tried, ok = Counter(), Counter()
    for r in op_counts:
        tried[r["type"]] += r["n"]
        if r["verdict"] == "accepted":
            ok[r["type"]] += r["n"]
    return [
        Finding(ERROR, "dead-capability", t,
                f"{tried[t]} attempted, never once accepted — the contract "
                f"offers this and the runtime has never honoured it")
        for t in sorted(tried)
        if tried[t] >= floor and ok[t] == 0
    ]


def parked_symbols(watchlist_statuses):
    """A watchlist row under a status the engine never reads.

    'active' is the only one that means anything: runner/ops.py will not price
    or trade a symbol without it, and jobs/classify_watchlist skips the rest.
    Seven rows sat in 'pending_engine' from 2026-07-27 to 2026-08-05 — a status
    nothing sets, reads or promotes — so TQQQ, SQQQ, SOXL, SOXS, SH, PSQ and
    SOL-USD were untradable, and nothing anywhere said so. Four house agents
    were chartered to trade through them in the meantime, and one of them
    (torque) may constitutionally trade nothing else.

    A symbol is either on the watchlist or it is not. A third state is a fossil,
    and this is what says so within the hour instead of nine days later.
    """
    return [
        Finding(ERROR, "parked-symbol", f"{r['status']} · {r['n']} symbol(s)",
                f"{r['symbols']} — the engine only ever reads status='active', "
                f"so these are untradable and nothing tells the agents why")
        for r in watchlist_statuses
        if r["status"] != "active"
    ]


def missing_journals(runs, repo, since=None, now=None):
    """Every completed session left an entry. Check the file is really there —
    the record is the published artifact, and Postgres saying 'completed' is not
    the same fact as a reader being able to read it.

    Both gates below say the same thing in two ways: only judge a run whose entry
    has had time to land. The working-copy gate alone was not enough. It reads
    HEAD's commit time as "everything older than this is present", which is not
    what HEAD's commit time means when four workflows push to the record from
    four clones: on 2026-08-12 some other agent's commit landed just before this
    job cloned, dragging the cutoff past the start of debut's run 977 while run
    977's own push was still in flight, and the tick went red for twenty-three
    minutes over a journal that was already on its way. Backing the cutoff off by
    JOURNAL_LANDS_WITHIN is what makes it mean what it was always read to mean.
    """
    if not repo or not repo.exists():
        return []
    cutoff = since if since is not None else checkout_time(repo)
    if cutoff is not None:
        cutoff -= JOURNAL_LANDS_WITHIN
    out = []
    for r in runs:
        if r["status"] != "completed":
            continue
        if cutoff is not None and r["started"] > cutoff:
            continue          # this working copy predates the entry
        if now is not None and now - r["started"] < JOURNAL_LANDS_WITHIN:
            continue          # too young to be called missing by any clone
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

@contextlib.contextmanager
def snapshot(conn):
    """Read the arena as it stood at one instant, not as it moved under us.

    book_against_fills compares cash and positions against the sum of every
    fill, and the engine writes both halves of that in one transaction — so the
    two are only ever inconsistent to a reader who looks at them at two
    different moments. Under Postgres's default READ COMMITTED, two sequential
    selects are two different moments, and a dispatch committing a trade between
    them would be enough to show half a trade as drift.

    This has never been observed, and it is NOT the explanation for the false
    drift of 2026-08-05 — that was a failed clone and missing launch snapshots,
    and check_book now refuses that case outright. The hole is real all the
    same, and it is the kind that would produce exactly the finding an operator
    must never learn to wave away.

    REPEATABLE READ pins one snapshot for the whole transaction, so the check
    compares halves of the same instant. Nothing here writes; the transaction is
    rolled back either way.
    """
    conn.rollback()          # the SET must be the first statement of the tx
    conn.execute("set transaction isolation level repeatable read")
    try:
        yield
    finally:
        conn.rollback()


def gather(conn, repo=None, head=None, now=None):
    now = now or datetime.now(timezone.utc)
    with snapshot(conn):
        return _read_and_check(conn, repo, head, now)


def _read_and_check(conn, repo, head, now):
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
    # Over the whole record on purpose, not the 7-day window: "has this ever
    # worked" is the question, and a window would answer "not lately".
    op_counts = conn.execute(
        "select type, verdict, count(*) n from operations group by type, verdict"
    ).fetchall()
    watchlist_statuses = conn.execute(
        """select status, count(*) n,
                  string_agg(symbol, ' ' order by symbol) symbols
           from watchlist group by status"""
    ).fetchall()

    findings = (
        stranded_journals(runs)
        + dead_capabilities(op_counts)
        + parked_symbols(watchlist_statuses)
        + wake_loops(triggers, now)
        + check_book(states, fills, launch_baselines(repo))
        + stuck_runs(runs, now)
        + stale_marks(states, now)
        + stale_triggers(triggers, now)
        + missing_journals(runs, repo, now=now)
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
