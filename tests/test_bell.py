"""The first bell's last stage: `done` may not be written until the published
floor actually serves the trader, because the status card turns that stage
into a "watch it on the floor" link the moment it lands."""
from jobs import bell


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def floor_of(*ids):
    return FakeResponse({"agents": [{"id": i} for i in ids]})


def patch_http(monkeypatch, responses):
    """Serve `responses` in order (an Exception instance is raised instead),
    repeating the last one forever. Records every call."""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(bell.requests, "get", fake_get)
    monkeypatch.setattr(bell.time, "sleep", lambda s: None)
    return calls


def test_floor_has_is_case_insensitive_and_exact(monkeypatch):
    patch_http(monkeypatch, [floor_of("ballast", "Rapid")])
    assert bell.floor_has("rapid") is True
    assert bell.floor_has("BALLAST") is True
    assert bell.floor_has("rap") is False
    assert bell.floor_has("nobody") is False


def test_awaits_the_deploy_then_returns(monkeypatch):
    """The engine pushes arena.json, but the floor is a build artifact — the
    trader shows up only once arena-web has redeployed, a minute or so later."""
    calls = patch_http(monkeypatch, [floor_of("ballast"),
                                     floor_of("ballast"),
                                     floor_of("ballast", "rapid")])
    assert bell.await_floor("rapid") is True
    assert len(calls) == 3


def test_a_slow_deploy_is_not_a_failed_bell(monkeypatch):
    """The entry is on the record either way; waiting forever would leave the
    principal watching a stepper that never finishes."""
    monkeypatch.setattr(bell, "FLOOR_WAIT_SECONDS", 0)
    patch_http(monkeypatch, [floor_of("ballast")])
    assert bell.await_floor("rapid") is False


def test_a_hiccup_reading_the_floor_is_retried(monkeypatch):
    calls = patch_http(monkeypatch, [RuntimeError("connection reset"),
                                     floor_of("ballast", "rapid")])
    assert bell.await_floor("rapid") is True
    assert len(calls) == 2


def test_the_bell_also_waits_for_a_sandbox_trader(monkeypatch):
    """A test seating is published beside the floor, not on it. The wait is the
    same wait — without this the first bell of every sandbox trader burns its
    whole 240s cap and reports a deploy that already landed."""
    published = FakeResponse({"agents": [{"id": "ballast"}],
                              "sandbox": [{"id": "probe"}]})
    patch_http(monkeypatch, [published])
    assert bell.floor_has("probe") is True
    assert bell.floor_has("ballast") is True
    assert bell.floor_has("nobody") is False


def test_a_payload_with_no_sandbox_key_still_works(monkeypatch):
    patch_http(monkeypatch, [floor_of("ballast")])
    assert bell.floor_has("ballast") is True
    assert bell.floor_has("probe") is False
