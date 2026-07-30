"""A session that dies at the model call is a market slot the agent never got.

Measured on the record 2026-07-30: 7 of 139 runs (5%) sat in 'started' forever,
every one with zero operations applied — so every one died here, in the model
call, before anything reached the book. One of the seven was a real principal's
first bell. The dispatcher deliberately never re-fires a crashed run, so there
was no second chance anywhere in the system.

Retrying HERE is the honest place: nothing has been applied and nothing
journalled, so asking the same question again is not repairing an answer.
"""
import pytest
import requests

from runner import brain


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(brain.time, "sleep", lambda *_: None)


class Reply:
    def __init__(self, status=200, payload=None, body=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = body

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def done(text="the answer"):
    return {"id": "i-1", "status": "completed", "usage": {},
            "steps": [{"type": "model_output", "content": [{"text": text}]}]}


def scripted(monkeypatch, posts, gets=()):
    """Replay canned replies (or raise canned exceptions) in order."""
    calls = {"post": 0, "get": 0}

    def take(seq, which):
        i = calls[which]
        calls[which] += 1
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", lambda *a, **k: take(posts, "post"))
    if gets:
        monkeypatch.setattr(requests, "get", lambda *a, **k: take(gets, "get"))
    return calls


# ---------- starting the interaction ----------

def test_a_transient_refusal_is_retried_and_the_run_survives(monkeypatch):
    calls = scripted(monkeypatch, [Reply(503, body="unavailable"), Reply(200, done())])
    text, _, iid = brain.run("persona", "task")
    assert text == "the answer" and iid == "i-1"
    assert calls["post"] == 2


def test_a_dropped_connection_is_retried(monkeypatch):
    calls = scripted(monkeypatch, [requests.ConnectionError("reset by peer"),
                                   Reply(200, done())])
    assert brain.run("persona", "task")[0] == "the answer"
    assert calls["post"] == 2


def test_rate_limiting_is_transient_not_fatal(monkeypatch):
    scripted(monkeypatch, [Reply(429, body="slow down"), Reply(200, done())])
    assert brain.run("persona", "task")[0] == "the answer"


def test_a_real_refusal_is_not_retried(monkeypatch):
    """A 400 will be a 400 again. Retrying it just spends the window."""
    calls = scripted(monkeypatch, [Reply(400, body="bad request")])
    with pytest.raises(brain.BrainError, match="400"):
        brain.run("persona", "task")
    assert calls["post"] == 1


def test_persistent_transient_failure_still_raises(monkeypatch):
    calls = scripted(monkeypatch, [Reply(503, body="unavailable")])
    with pytest.raises(brain.BrainError, match="could not start interaction"):
        brain.run("persona", "task")
    assert calls["post"] == 3


# ---------- following it ----------

def test_a_failed_poll_does_not_kill_a_running_interaction(monkeypatch):
    """The interaction is running on someone else's machine. A poll that fails
    says nothing about it."""
    running = {"id": "i-1", "status": "in_progress"}
    scripted(monkeypatch,
             [Reply(200, running)],
             [requests.Timeout("read timeout"), Reply(200, done())])
    assert brain.run("persona", "task")[0] == "the answer"


def test_losing_contact_for_good_is_reported_as_that(monkeypatch):
    running = {"id": "i-1", "status": "in_progress"}
    scripted(monkeypatch, [Reply(200, running)],
             [requests.Timeout("read timeout")])
    with pytest.raises(brain.BrainError, match="lost contact"):
        brain.run("persona", "task")


def test_a_poll_returning_junk_counts_as_a_miss_not_a_verdict(monkeypatch):
    running = {"id": "i-1", "status": "in_progress"}
    scripted(monkeypatch, [Reply(200, running)],
             [Reply(200, ValueError("not json")), Reply(200, done())])
    assert brain.run("persona", "task")[0] == "the answer"


def test_a_failed_interaction_is_never_dressed_up_as_an_answer(monkeypatch):
    scripted(monkeypatch, [Reply(200, {"id": "i-1", "status": "failed"})])
    with pytest.raises(brain.BrainError, match="status failed"):
        brain.run("persona", "task")


def test_a_completed_interaction_with_no_text_is_a_failure(monkeypatch):
    """Silence is not a session. Better a failed run than an empty entry."""
    scripted(monkeypatch, [Reply(200, {"id": "i-1", "status": "completed",
                                       "steps": []})])
    with pytest.raises(brain.BrainError, match="no model_output"):
        brain.run("persona", "task")
