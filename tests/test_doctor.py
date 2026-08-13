"""The standing check, checked.

A monitor nobody trusts is worse than none: the first version of
book_against_fills reported $80,000 of unexplained cash on vertex, and it was
right about the arithmetic and wrong about the arena — that money was the
2026-07-22 launch snapshot, seeded straight into the tables before any fill
existed to explain it. A false alarm that fires every tick teaches an operator
to ignore the one that matters, so the baseline is part of the contract and has
a test of its own.
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from jobs import doctor

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def run(rid=1, agent="tempo", status="completed", ago_h=1, trigger="scheduled",
        journal_title=None):
    return {"id": rid, "agent_id": agent, "status": status, "trigger": trigger,
            "started": NOW - timedelta(hours=ago_h), "journal_title": journal_title}


def trig(agent="tempo", kind="drawdown", ago_h=1, handled=True):
    return {"agent_id": agent, "kind": kind, "handled": handled,
            "ts": NOW - timedelta(hours=ago_h)}


def fill(agent="tempo", symbol="AMD", side="buy", qty=10.0, price=100.0):
    return {"agent_id": agent, "symbol": symbol, "side": side,
            "qty": qty, "fill_price": price}


def state(agent="tempo", cash=100_000.0, positions=None, launched="2026-07-22",
          last_mark_h=1):
    return {"agent_id": agent, "cash": cash, "launched": launched,
            "positions": positions or {},
            "last_mark": NOW - timedelta(hours=last_mark_h)}


def levels(findings):
    return sorted((f.level, f.check) for f in findings)


# ---------- the book must equal the fills ----------

def test_a_book_that_matches_its_fills_is_silent():
    fills = [fill(qty=10, price=100)]           # $1,000 out, 10 shares in
    states = [state(cash=99_000.0, positions={"AMD": 10.0})]
    assert doctor.book_against_fills(states, fills) == []


def test_cash_that_moved_without_a_fill_is_an_error():
    """The one class of bug that would make every published number suspect."""
    states = [state(cash=95_000.0, positions={"AMD": 10.0})]
    findings = doctor.book_against_fills(states, [fill(qty=10, price=100)])
    assert [f.check for f in findings] == ["book-drift"]
    assert findings[0].level == doctor.ERROR
    assert "-4,000.00 unexplained" in findings[0].detail


def test_a_position_that_does_not_match_its_fills_is_an_error():
    states = [state(cash=99_000.0, positions={"AMD": 14.0})]
    findings = doctor.book_against_fills(states, [fill(qty=10, price=100)])
    assert findings[0].subject == "tempo · AMD"


def test_a_position_held_with_no_fills_at_all_is_caught():
    findings = doctor.book_against_fills([state(positions={"NVDA": 5.0})], [])
    assert findings and findings[0].level == doctor.ERROR


def test_a_sell_returns_the_proceeds():
    fills = [fill(side="buy", qty=10, price=100), fill(side="sell", qty=10, price=110)]
    states = [state(cash=100_100.0, positions={})]
    assert doctor.book_against_fills(states, fills) == []


def test_float_dust_is_not_a_finding():
    states = [state(cash=99_000.004, positions={"AMD": 10.0000000001})]
    assert doctor.book_against_fills(states, [fill(qty=10, price=100)]) == []


def test_the_launch_snapshot_is_the_baseline_not_a_drift():
    """vertex opened on 2026-07-22 holding three names and $20,000, none of it
    from a fill. That is the starting balance, not $80,000 gone missing."""
    baselines = {"vertex": {"cash": 20_000.0,
                            "pos": Counter({"NVDA": 164.9249, "TSM": 58.8712})}}
    states = [state("vertex", cash=20_000.0,
                    positions={"NVDA": 164.9249, "TSM": 58.8712})]
    assert doctor.book_against_fills(states, [], baselines) == []


def test_a_seeded_position_sold_later_still_balances():
    baselines = {"vertex": {"cash": 20_000.0, "pos": Counter({"AMZN": 81.9486})}}
    fills = [fill("vertex", "AMZN", "sell", 81.9486, 200.0)]
    states = [state("vertex", cash=20_000.0 + 81.9486 * 200.0, positions={})]
    assert doctor.book_against_fills(states, fills, baselines) == []


def test_baselines_are_read_from_the_launch_snapshots(tmp_path):
    d = tmp_path / "agents" / "vertex"
    d.mkdir(parents=True)
    (d / "portfolio.json").write_text(json.dumps({
        "cash": 20_000.0, "peak_equity": 100_000.0,
        "positions": [{"symbol": "NVDA", "qty": 164.9249}]}))
    (tmp_path / "agents" / "catalyst").mkdir()
    base = doctor.launch_baselines(tmp_path)
    assert base["vertex"]["cash"] == 20_000.0
    assert base["vertex"]["pos"]["NVDA"] == 164.9249
    assert "catalyst" not in base          # no snapshot: starts at the full float


def test_a_record_that_could_not_be_read_is_not_an_empty_record(tmp_path):
    """{} means nobody was seeded; None means nobody looked. On 2026-08-05 a
    failed clone made those the same value."""
    assert doctor.launch_baselines(tmp_path / "nope") is None
    assert doctor.launch_baselines(None) is None
    (tmp_path / "agents").mkdir()
    assert doctor.launch_baselines(tmp_path) == {}


def test_the_book_is_not_checked_against_snapshots_nobody_could_read():
    """The exact 2026-08-05 shape: vertex's launch book, no baselines to explain
    it. Eight ERRORs went out that day; the honest answer is that the check did
    not run."""
    states = [state("vertex", cash=20_000.0, positions={"NVDA": 164.9249})]
    assert doctor.book_against_fills(states, [], {}), \
        "without the refusal this is the false alarm that fired"
    findings = doctor.check_book(states, [], None)
    assert [f.check for f in findings] == ["book-uncheckable"]
    assert "nobody checked it" in findings[0].detail


def test_a_readable_record_is_still_checked():
    baselines = {"vertex": {"cash": 20_000.0, "pos": Counter({"NVDA": 164.9249})}}
    states = [state("vertex", cash=20_000.0, positions={"NVDA": 164.9249})]
    assert doctor.check_book(states, [], baselines) == []
    moved = [state("vertex", cash=15_000.0, positions={"NVDA": 164.9249})]
    assert [f.check for f in doctor.check_book(moved, [], baselines)] == ["book-drift"]


def test_an_unreadable_record_is_loud_not_quiet():
    """A check that silently did not run is how eight days of phantom drift got
    filed as a race. It has to cost an exit code."""
    findings = doctor.check_book([], [], None)
    assert findings[0].level == doctor.ERROR
    assert doctor.report(findings) == 1


def test_an_agent_born_after_launch_starts_from_the_standard_float():
    """Seated traders have no snapshot — they start at $100,000 and every move
    after that is a fill."""
    states = [state("ballast", cash=100_000.0)]
    assert doctor.book_against_fills(states, [], {}) == []


# ---------- the loop that started this ----------

def test_a_condition_refiring_all_night_is_an_error():
    triggers = [trig(ago_h=h) for h in range(1, 14)]     # the real night: 13
    findings = doctor.wake_loops(triggers, NOW)
    assert [f.check for f in findings] == ["wake-loop"]
    assert findings[0].level == doctor.ERROR
    assert "13 drawdown triggers in 24h" in findings[0].detail


def test_normal_waking_is_not_a_loop():
    triggers = [trig(kind="stop_filled", ago_h=2), trig(kind="drawdown", ago_h=3),
                trig(kind="position_closed", ago_h=4)]
    assert doctor.wake_loops(triggers, NOW) == []


def test_yesterdays_wakes_do_not_count_against_today():
    assert doctor.wake_loops([trig(ago_h=h) for h in range(25, 40)], NOW) == []


def test_a_loop_that_has_stopped_is_no_longer_reported():
    """The count survives 24 hours after a fix. Failing every tick over
    something already dealt with is how a checker gets ignored — so a loop must
    also be current to be a finding."""
    stopped = [trig(ago_h=h) for h in range(6, 20)]     # nothing in 6 hours
    assert doctor.wake_loops(stopped, NOW) == []


def test_a_loop_still_firing_is_reported_with_how_recently():
    live = [trig(ago_h=h) for h in range(1, 15)]
    findings = doctor.wake_loops(live, NOW)
    assert findings and "1.0h ago" in findings[0].detail


def test_the_loop_is_found_per_agent_and_per_kind():
    triggers = ([trig("tempo", ago_h=h) for h in range(1, 8)]
                + [trig("vertex", ago_h=h) for h in range(1, 9)]
                + [trig("rapid", "stop_filled", ago_h=1)])
    subjects = [f.subject for f in doctor.wake_loops(triggers, NOW)]
    assert subjects == ["tempo · drawdown", "vertex · drawdown"]


# ---------- sessions that died ----------

def test_a_session_still_started_hours_later_is_reported():
    findings = doctor.stuck_runs([run(status="started", ago_h=20)], NOW)
    assert findings[0].level == doctor.WARN
    assert "20.0h" in findings[0].detail


def test_a_session_in_flight_is_left_alone():
    assert doctor.stuck_runs([run(status="started", ago_h=0.2)], NOW) == []


def test_a_completed_session_is_never_stuck():
    assert doctor.stuck_runs([run(status="completed", ago_h=40)], NOW) == []


def test_a_journal_held_in_postgres_by_a_dead_run_is_an_error():
    """The operations were applied and the entry may never have been filed. The
    text is recoverable, which is the whole point of reporting it."""
    findings = doctor.stranded_journals(
        [run(status="failed", journal_title="the semis rout, revisited")])
    assert findings[0].level == doctor.ERROR
    assert "the semis rout" in findings[0].detail


def test_a_completed_run_holds_no_stranded_journal():
    assert doctor.stranded_journals([run(status="completed",
                                         journal_title="filed")]) == []


# ---------- the record and the floor ----------

def test_a_completed_run_with_no_entry_in_the_record_is_an_error(tmp_path):
    (tmp_path / "agents" / "tempo" / "journal").mkdir(parents=True)
    findings = doctor.missing_journals([run(agent="tempo", ago_h=1)], tmp_path)
    assert findings[0].check == "missing-entry"
    assert "2026-07-30.md" in findings[0].detail


def test_an_entry_that_is_there_is_not_reported(tmp_path):
    d = tmp_path / "agents" / "tempo" / "journal"
    d.mkdir(parents=True)
    (d / "2026-07-30.md").write_text("# entry\n")
    assert doctor.missing_journals([run(agent="tempo", ago_h=1)], tmp_path) == []


def test_no_repo_means_no_opinion_about_the_record(tmp_path):
    assert doctor.missing_journals([run()], tmp_path / "nope") == []


def test_a_run_newer_than_this_working_copy_is_not_missing(tmp_path):
    """Run from a laptop two hours behind the record, the check reported a real
    principal's first session as missing. A run that completed after this copy
    was taken cannot be in it."""
    (tmp_path / "agents" / "ledger" / "journal").mkdir(parents=True)
    taken = NOW - timedelta(hours=3)
    assert doctor.missing_journals(
        [run(agent="ledger", ago_h=1)], tmp_path, since=taken) == []
    # ...and a run from before it is still judged
    assert doctor.missing_journals(
        [run(agent="ledger", ago_h=5)], tmp_path, since=taken) != []


def test_an_entry_still_on_its_way_is_not_called_missing(tmp_path):
    """2026-08-12: the tick went red on debut's run 977 and the next tick, with
    no commit to main in between, was clean. Four workflows push the record from
    four clones; another agent's commit landing just before this one cloned
    dragged the cutoff past a run whose own push had not finished."""
    (tmp_path / "agents" / "debut" / "journal").mkdir(parents=True)
    cloned = NOW - timedelta(minutes=2)      # someone else's commit, just now
    assert doctor.missing_journals(
        [run(agent="debut", ago_h=0.1)], tmp_path, since=cloned) == []


def test_an_entry_that_never_landed_is_still_reported(tmp_path):
    """The grace delays the finding by one tick. It does not forgive it."""
    (tmp_path / "agents" / "debut" / "journal").mkdir(parents=True)
    cloned = NOW - timedelta(minutes=2)
    assert doctor.missing_journals(
        [run(agent="debut", ago_h=3)], tmp_path, since=cloned, now=NOW) != []


def test_a_session_that_just_ended_is_too_young_to_judge(tmp_path):
    """True whatever the working copy says — with no git metadata at all there
    is no cutoff, and the wall clock is the only thing left to be patient with."""
    (tmp_path / "agents" / "debut" / "journal").mkdir(parents=True)
    fresh = [run(agent="debut", ago_h=0.05)]
    assert doctor.missing_journals(fresh, tmp_path, now=NOW) == []
    assert doctor.missing_journals(fresh, tmp_path) != [], \
        "without a clock the old behaviour stands, so this is the clock's doing"


def test_the_checkout_time_is_read_from_the_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    for args in (["add", "f"],
                 ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(tmp_path)] + args, check=True)
    assert doctor.checkout_time(tmp_path) is not None
    assert doctor.checkout_time(tmp_path / "not-a-repo") is None


def test_a_trader_with_no_rendered_face_is_reported_with_the_fix():
    findings = doctor.missing_faces(["vector"], lambda url: 404)
    assert findings[0].check == "missing-face"
    assert "gen-avatars.mjs vector" in findings[0].detail


def test_faces_that_serve_are_quiet():
    assert doctor.missing_faces(["ballast", "fury"], lambda url: 200) == []


def test_a_network_failure_is_a_warning_about_the_check_not_the_trader():
    def dead(url):
        raise OSError("dns is down")
    findings = doctor.missing_faces(["ballast"], dead)
    assert findings[0].check == "face-check" and findings[0].level == doctor.WARN


def test_a_frozen_mark_is_reported():
    findings = doctor.stale_marks([state(last_mark_h=20)], NOW)
    assert findings[0].check == "stale-mark"


def test_an_unlaunched_agent_is_not_expected_to_be_marked():
    assert doctor.stale_marks([state(launched=None, last_mark_h=200)], NOW) == []


def test_a_wake_nobody_answered_is_reported():
    findings = doctor.stale_triggers([trig(handled=False, ago_h=9)], NOW)
    assert findings[0].check == "unanswered-wake"


def test_a_fresh_unhandled_wake_is_just_a_wake():
    assert doctor.stale_triggers([trig(handled=False, ago_h=0.5)], NOW) == []


# ---------- reading one instant, not two ----------
#
# 2026-08-05: doctor reported $25,000, $25,000 and $80,000 of unexplained cash
# across maverick, tempo and vertex, and the next tick reported nothing, with no
# commit to main in between. The money never moved. gather read agent_state and
# fills as two sequential selects, so a dispatch committing a trade between them
# showed up as half a trade.

class RecordingConn:
    """A connection that answers every select with nothing and remembers the
    order it was asked."""

    def __init__(self):
        self.statements = []
        self.rollbacks = 0

    def execute(self, sql, args=None):
        self.statements.append(" ".join(str(sql).split()).lower())
        return self

    def fetchall(self):
        return []

    def rollback(self):
        self.rollbacks += 1
        self.statements.append("rollback")


def test_the_book_and_its_fills_are_read_from_one_snapshot():
    conn = RecordingConn()
    doctor.gather(conn)
    first = conn.statements[0] if conn.statements[0] != "rollback" else conn.statements[1]
    assert first == "set transaction isolation level repeatable read", \
        "the isolation level only takes if it opens the transaction"
    reads = [s for s in conn.statements if s.startswith("select")]
    assert any("from agent_state" in s for s in reads)
    assert any("from fills" in s for s in reads)


def test_the_snapshot_is_closed_even_when_a_check_raises():
    """doctor holds a transaction open over every read it does. Leaving one open
    on the way out is how a connection ends up idle-in-transaction and killed."""
    conn = RecordingConn()
    try:
        with doctor.snapshot(conn):
            raise RuntimeError("a check blew up")
    except RuntimeError:
        pass
    assert conn.statements[-1] == "rollback"
    assert conn.rollbacks == 2      # one to open cleanly, one to let go


def test_doctor_never_writes_while_it_reads():
    conn = RecordingConn()
    doctor.gather(conn)
    assert not [s for s in conn.statements
                if s.startswith(("insert", "update", "delete"))]
    assert conn.statements[-1] == "rollback"


# ---------- the report ----------

def test_a_clean_arena_exits_zero():
    assert doctor.report([]) == 0


def test_errors_exit_nonzero_and_warnings_do_not():
    warn = [doctor.Finding(doctor.WARN, "stuck-run", "tempo", "d")]
    err = [doctor.Finding(doctor.ERROR, "book-drift", "tempo", "d")]
    assert doctor.report(warn) == 0
    assert doctor.report(err + warn) == 1


def test_errors_are_printed_before_warnings(capsys):
    doctor.report([doctor.Finding(doctor.WARN, "stuck-run", "a", "d"),
                   doctor.Finding(doctor.ERROR, "book-drift", "b", "d")])
    out = capsys.readouterr().out
    assert out.index("book-drift") < out.index("stuck-run")


# ---------- a promise the runtime never kept ----------

def ops(type, accepted=0, rejected=0):
    out = []
    if accepted:
        out.append({"type": type, "verdict": "accepted", "n": accepted})
    if rejected:
        out.append({"type": type, "verdict": "rejected", "n": rejected})
    return out


def test_an_operation_refused_every_single_time_is_a_broken_promise():
    """The watchlist case, as it really stood on 2026-07-31."""
    found = doctor.dead_capabilities(ops("watchlist_request", rejected=12))
    assert [f.check for f in found] == ["dead-capability"]
    assert found[0].level == doctor.ERROR
    assert found[0].subject == "watchlist_request"
    assert "12 attempted" in found[0].detail


def test_a_symbol_parked_under_an_unread_status_is_a_finding():
    """The 2026-07-27 fossil, as it really stood for nine days: seven rows in
    'pending_engine', a status nothing sets, reads or promotes, while four
    house agents were chartered to trade through them."""
    found = doctor.parked_symbols([
        {"status": "active", "n": 101, "symbols": "AAPL AMD SPY"},
        {"status": "pending_engine", "n": 7, "symbols": "PSQ SH SOL-USD SOXL"},
    ])
    assert [f.check for f in found] == ["parked-symbol"]
    assert found[0].level == doctor.ERROR
    assert "SOXL" in found[0].detail and "pending_engine" in found[0].subject


def test_a_watchlist_that_is_entirely_active_is_silent():
    assert doctor.parked_symbols(
        [{"status": "active", "n": 108, "symbols": "AAPL AMD"}]) == []


def test_one_acceptance_anywhere_in_the_record_clears_it():
    """A capability that works and is often refused is the constitution doing
    its job, which is the opposite of a finding."""
    assert doctor.dead_capabilities(
        ops("place_order", accepted=1, rejected=99)) == []


def test_a_capability_barely_tried_is_not_yet_evidence():
    """Two refusals is an agent being told no. The floor exists so the check
    does not shout at a contract nobody has exercised."""
    assert doctor.dead_capabilities(ops("guidance_response", rejected=2)) == []
    assert len(doctor.dead_capabilities(
        ops("guidance_response", rejected=doctor.DEAD_CAPABILITY_FLOOR))) == 1


def test_each_operation_type_is_judged_on_its_own_record():
    found = doctor.dead_capabilities(
        ops("place_order", accepted=50, rejected=10)
        + ops("watchlist_request", rejected=12)
        + ops("cancel_order", accepted=8))
    assert [f.subject for f in found] == ["watchlist_request"]


def test_a_quiet_record_says_nothing():
    assert doctor.dead_capabilities([]) == []
