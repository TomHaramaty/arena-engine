"""A session that dies says so, and the entry it wrote is not lost with it.

Two things were true of a crashed run before 2026-07-30: its row stayed
'started' forever, indistinguishable from a session still in flight (seven of
them had piled up), and if it died between applying the operations and pushing
the journal, the explanation for real trades existed nowhere. The dispatcher
never re-fires a crashed run, so nothing would ever have written it.
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


def test_a_brain_that_cannot_be_read_twice_fails_the_session():
    """Retrying a flake is not the same as papering over a broken brain."""
    run, calls = replies(BAD, BAD)
    with pytest.raises(ops.OpsParseError):
        agent_run._deliberate("ballast", "persona", "task", run=run)
    assert len(calls) == agent_run.DELIBERATION_ATTEMPTS
