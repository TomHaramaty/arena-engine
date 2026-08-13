"""A session that dies says so, and the entry it wrote is not lost with it.

Two things were true of a crashed run before 2026-07-30: its row stayed
'started' forever, indistinguishable from a session still in flight (seven of
them had piled up), and if it died between applying the operations and pushing
the journal, the explanation for real trades existed nowhere. The dispatcher
re-fires a crashed run only when the row says the book was never touched, so
for anything past that point nothing would ever have written it.
"""
import json

import pytest

from engine import gitrepo
from jobs import agent_run
from runner import brain, context, ops
from tests.fakedb import FakeConn


class Boom(RuntimeError):
    pass


@pytest.fixture
def arena(monkeypatch):
    """An agent whose context and journal-writing are stubbed, so the tests are
    about the failure bookkeeping and nothing else."""
    monkeypatch.setattr(context, "build_agents_md", lambda aid: "persona")
    monkeypatch.setattr(context, "build_task", lambda conn, aid: ("task", 100_000.0))
    pushed = []
    monkeypatch.setattr(agent_run, "commit_journal",
                        lambda *a: pushed.append(a))
    return pushed


def brain_says(monkeypatch, text):
    monkeypatch.setattr(brain, "run", lambda *a, **k: (text, {}, "i-1"))


def block(*operations):
    return "```json\n" + json.dumps({"operations": list(operations)}) + "\n```"


JOURNAL = {"type": "journal_entry", "title": "a quiet day",
           "body_markdown": "nothing fired."}


def conn():
    return FakeConn(agent_id="tempo", watchlist={"AMD": "equity"},
                    ticks={"AMD": 100.0})


# ---------- the epitaph ----------

def test_a_dead_session_is_marked_failed_with_its_reason(arena, monkeypatch):
    monkeypatch.setattr(brain, "run", lambda *a, **k: (_ for _ in ()).throw(
        brain.BrainError("HTTP 503: unavailable")))
    c = conn()
    with pytest.raises(brain.BrainError):
        agent_run.run_agent(c, "tempo")
    assert c.runs[0]["status"] == "failed"
    assert "503" in c.runs[0]["meta"]["error"]


def test_the_exception_still_reaches_the_dispatcher(arena, monkeypatch):
    """Recording the failure must not swallow it — the dispatcher counts
    failures and the job's exit code is how anyone finds out."""
    monkeypatch.setattr(brain, "run", lambda *a, **k: (_ for _ in ()).throw(Boom("x")))
    with pytest.raises(Boom):
        agent_run.run_agent(conn(), "tempo")


def test_an_unparseable_answer_is_a_failed_run_not_a_silent_one(arena, monkeypatch):
    brain_says(monkeypatch, "I would rather not use the format.")
    c = conn()
    with pytest.raises(ops.OpsParseError):
        agent_run.run_agent(c, "tempo")
    assert c.runs[0]["status"] == "failed"


# ---------- whether the backup may give the session back ----------

def test_a_block_that_never_parsed_left_the_book_untouched(arena, monkeypatch):
    """The parse is the gate: nothing was ordered and nothing journalled, so
    the dispatcher's backup may re-fire this one."""
    brain_says(monkeypatch, "I would rather not use the format.")
    c = conn()
    with pytest.raises(ops.OpsParseError):
        agent_run.run_agent(c, "tempo")
    assert c.runs[0]["meta"]["retryable"] is True


def test_any_other_death_is_assumed_to_have_moved_the_book(arena, monkeypatch):
    """A session that died anywhere else may have placed an order first. It is
    never re-fired, however cheap the failure looks."""
    monkeypatch.setattr(brain, "run", lambda *a, **k: (_ for _ in ()).throw(
        brain.BrainError("HTTP 503: unavailable")))
    c = conn()
    with pytest.raises(brain.BrainError):
        agent_run.run_agent(c, "tempo")
    assert c.runs[0]["meta"]["retryable"] is False


def test_the_untouched_book_test_is_the_exception_not_the_message():
    assert agent_run.untouched_book(ops.OpsParseError("no fenced json block"))
    assert not agent_run.untouched_book(RuntimeError(
        "the operations block is not valid json"))


def test_a_dry_run_leaves_no_row_to_mark(arena, monkeypatch):
    monkeypatch.setattr(brain, "run", lambda *a, **k: (_ for _ in ()).throw(Boom("x")))
    c = conn()
    with pytest.raises(Boom):
        agent_run.run_agent(c, "tempo", dry=True)
    assert c.runs == []


def test_marking_failed_never_raises_over_the_original_failure():
    """If even the epitaph cannot be written, the caller still sees the real
    exception rather than a database error standing in front of it."""
    class Hostile(FakeConn):
        def execute(self, sql, args=None):
            if "update runs" in " ".join(str(sql).split()).lower():
                raise RuntimeError("connection is bad")
            return super().execute(sql, args)

    c = Hostile(agent_id="tempo")
    agent_run.mark_failed(c, 1, Boom("the real problem"))   # must not raise


# ---------- the entry survives a push that fails ----------

def test_the_entry_is_in_postgres_before_it_is_pushed(arena, monkeypatch):
    """The order matters: operations are already committed by now, so the entry
    must exist somewhere before the push is attempted."""
    seen = {}
    brain_says(monkeypatch, block(JOURNAL))

    def watchful_push(*a):
        seen["meta"] = json.loads(json.dumps(c.runs[0]["meta"]))

    monkeypatch.setattr(agent_run, "commit_journal", watchful_push)
    c = conn()
    agent_run.run_agent(c, "tempo")
    assert seen["meta"]["journal"]["title"] == "a quiet day"
    assert seen["meta"]["journal"]["body_markdown"] == "nothing fired."


def test_an_unpushable_entry_is_left_recoverable_on_a_failed_run(arena, monkeypatch):
    """The book moved and the record could not be written. doctor reads this
    back and reports it; the text is right there to file from."""
    brain_says(monkeypatch, block(JOURNAL))
    monkeypatch.setattr(agent_run, "commit_journal", lambda *a: (_ for _ in ()).throw(
        gitrepo.PushError("could not push to the record after 4 attempts")))
    c = conn()
    with pytest.raises(gitrepo.PushError):
        agent_run.run_agent(c, "tempo")
    assert c.runs[0]["status"] == "failed"
    assert c.runs[0]["meta"]["journal"]["title"] == "a quiet day"


def test_a_completed_run_does_not_keep_two_copies_of_its_entry(arena, monkeypatch):
    """Once the entry is in the record, that is where published prose lives."""
    brain_says(monkeypatch, block(JOURNAL))
    c = conn()
    agent_run.run_agent(c, "tempo")
    assert c.runs[0]["status"] == "completed"
    assert "journal" not in c.runs[0]["meta"]
    assert c.runs[0]["meta"]["interaction_id"] == "i-1"


# ------------------------------------------------- asking again for the block
#
# 2026-07-31: a real principal's trader lost its first session twice to an
# operations block that would not parse, and parsed perfectly the next time it
# was asked. At that point in a session nothing has been applied and nothing
# journalled, which is the same state in which brain.py already retries.

GOOD = '```json\n{"operations": [{"type": "journal_entry", "title": "t", "body_markdown": "b"}]}\n```'
BAD = '```json\n{"operations": [oops]}\n```'

#: helix, 2026-08-12 20:53 UTC, verbatim in shape: everything between
#: `"operations":` and the middle of the journal body is missing, and the tail
#: closes as if nothing had happened.
GROUNDED_HOLE = ('```json\n{"operations":. Supported by Fast Track designation'
                 ' and two pivotal Phase 3 trials.\\n\\n## Rationale\\n- The'
                 ' stock is trading at $100.73."}\n]}\n```')
USAGE = {"total_input_tokens": 100, "total_output_tokens": 10, "total_thought_tokens": 5}


def replies(*texts):
    """A brain that answers with each text in turn, counting its calls."""
    calls = []

    def run(agents_md, task):
        calls.append(task)
        return texts[len(calls) - 1], USAGE, f"i{len(calls)}"
    return run, calls


def test_a_readable_block_is_asked_for_once():
    run, calls = replies(GOOD)
    parsed, spent, cost = agent_run._deliberate("ballast", "persona", "task", run=run)
    assert parsed[0]["type"] == "journal_entry"
    assert len(calls) == 1
    assert spent["interaction_id"] == "i1"


def test_an_unreadable_block_is_asked_again():
    run, calls = replies(BAD, GOOD)
    parsed, spent, cost = agent_run._deliberate("ballast", "persona", "task", run=run)
    assert parsed[0]["title"] == "t"
    assert len(calls) == 2
    assert calls[0] == calls[1], "the same question, not a repaired one"
    assert spent["interaction_id"] == "i2", "the interaction that was actually read"


def test_both_attempts_are_paid_for_and_counted():
    """A retried session spent two interactions; the record must say so."""
    run, _ = replies(BAD, GOOD)
    _, spent, cost = agent_run._deliberate("ballast", "persona", "task", run=run)
    assert spent["in"] == 200
    assert spent["out"] == 30
    assert cost == round(2 * brain.cost_usd(USAGE), 4)


def test_a_brain_that_cannot_be_read_at_all_fails_the_session():
    """Retrying a flake is not the same as papering over a broken brain."""
    run, calls = replies(*[BAD] * agent_run.DELIBERATION_ATTEMPTS)
    with pytest.raises(ops.OpsParseError):
        agent_run._deliberate("ballast", "persona", "task", run=run)
    assert len(calls) == agent_run.DELIBERATION_ATTEMPTS


def test_the_grounded_flake_is_asked_past_rather_than_lost():
    """Gemini's grounded answers came back with a hole in them 10 times in 45 on
    2026-08-12 — the block resumes mid-sentence right after `"operations":`.
    Two attempts lost three sessions that day; the fourth attempt is what makes
    a run of bad luck cost a retry instead of a market slot."""
    assert agent_run.DELIBERATION_ATTEMPTS >= 4
    run, calls = replies(GROUNDED_HOLE, BAD, BAD, GOOD)
    parsed, spent, _ = agent_run._deliberate("ballast", "persona", "task", run=run)
    assert parsed[0]["title"] == "t"
    assert len(calls) == 4
    assert spent["interaction_id"] == "i4"


def test_the_hole_in_a_grounded_answer_is_unreadable_not_half_read():
    """It must fail the parse outright. The one thing worse than losing the
    session is filing a journal entry that starts mid-sentence."""
    with pytest.raises(ops.OpsParseError):
        ops.parse(GROUNDED_HOLE)
