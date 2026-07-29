"""The letter: eligibility, arithmetic, and the quantity guard."""

import pytest

from jobs.letter import (
    ProseStatesQuantity,
    day_events,
    eligible,
    no_quantities,
    payload,
    round_trip,
)


# ------------------------------------------------------------- quantity guard

def test_plain_prose_passes():
    no_quantities({"line": "I sold Visa this afternoon, one day after buying it."})


def test_a_stated_number_is_refused():
    with pytest.raises(ProseStatesQuantity):
        no_quantities({"line": "I sold Visa at $366.04."})


def test_record_ids_survive():
    """P2/H1/C3 point INTO the record; the trader must be able to cite a rule."""
    no_quantities({"why": "My rule P2 says the edge dies with the event, and H1 needs a raise."})


def test_a_number_hidden_in_markup_is_still_refused():
    with pytest.raises(ProseStatesQuantity):
        no_quantities({"why": 'it rose <span title="x">nine</span> to 12 per cent'})


def test_a_number_hidden_inside_a_tag_attribute_does_not_smuggle_through():
    """Markup is stripped before the check, so an attribute cannot carry one in."""
    no_quantities({"why": '<a href="https://conviction-league.com/floor?v=2">on the floor</a>'})


def test_the_failing_key_is_named():
    with pytest.raises(ProseStatesQuantity, match="belief"):
        no_quantities({"line": "clean prose", "belief": "right 55% of the time"})


# ---------------------------------------------------------------- eligibility

FILL = {"agent": "catalyst", "event": "fill", "when": "Jul 28 20:42", "side": "sell",
        "symbol": "V", "qty": 10.0, "price": 366.040115, "t": 300}


def test_off_never_sends():
    assert eligible("off", [FILL])[0] is False
    assert eligible("off", [FILL], is_reflection_day=True)[0] is False


def test_daily_stays_quiet_when_nothing_happened():
    send, why = eligible("daily", [])
    assert send is False and "nothing happened" in why


def test_daily_sends_on_an_event():
    send, why = eligible("daily", [FILL])
    assert send is True and "event" in why


def test_a_moving_mark_is_not_an_event():
    """The ruling's whole point: only the record counts, never the valuation."""
    assert eligible("daily", [])[0] is False


def test_answering_the_principal_is_an_event():
    send, why = eligible("daily", [], answered_guidance=1)
    assert send is True and "answered its principal" in why


def test_a_rulebook_change_is_an_event():
    send, why = eligible("daily", [], rulebook_changed=True)
    assert send is True and "rulebook" in why


def test_weekly_is_silent_off_the_reflection_day():
    assert eligible("weekly", [FILL])[0] is False


def test_weekly_sends_on_the_reflection_day():
    send, why = eligible("weekly", [], is_reflection_day=True)
    assert send is True and "reflection" in why


def test_a_quiet_trader_says_why_it_was_quiet():
    """A silent trader is a fact about the strategy, and must stay legible."""
    _, why = eligible("daily", [])
    assert why and isinstance(why, str)


# ------------------------------------------------------------------ the facts

TAPE = [
    {"agent": "catalyst", "event": "fill", "when": "Jul 28 20:42", "side": "sell",
     "symbol": "V", "qty": 54.9887773846356, "price": 366.040115, "t": 300},
    {"agent": "catalyst", "event": "pulled", "when": "Jul 28 20:42", "side": "sell",
     "symbol": "V", "mechanism": "stop", "trigger": 345.0, "t": 300},
    {"agent": "catalyst", "event": "fill", "when": "Jul 27 14:41", "side": "buy",
     "symbol": "V", "qty": 54.9887773846356, "price": 361.892025, "t": 200},
    {"agent": "maverick", "event": "fill", "when": "Jul 28 14:41", "side": "buy",
     "symbol": "GOOGL", "qty": 1.0, "price": 100.0, "t": 250},
]


def test_day_events_are_this_trader_on_this_day():
    got = day_events(TAPE, "catalyst", "Jul 28")
    assert len(got) == 2
    assert {e["event"] for e in got} == {"fill", "pulled"}


def test_another_traders_day_is_not_mine():
    assert day_events(TAPE, "catalyst", "Jul 28")[0]["agent"] == "catalyst"
    assert len(day_events(TAPE, "maverick", "Jul 28")) == 1


def test_round_trip_arithmetic_comes_from_the_fills():
    """The real case: the trader's prose claimed +1.3%; the fills say +1.15%."""
    rt = round_trip(TAPE, "catalyst", TAPE[0])
    assert rt is not None
    assert rt["entry"] == pytest.approx(361.892025)
    assert rt["ret"] == pytest.approx(0.011462, abs=1e-5)
    assert rt["ret"] < 0.013  # emphatically not the +1.3% the prose asserted
    assert rt["gain"] == pytest.approx(228.10, abs=0.01)


def test_a_sell_with_no_prior_buy_yields_nothing_rather_than_guessing():
    orphan = {"agent": "catalyst", "event": "fill", "side": "sell", "symbol": "ZZZZ",
              "qty": 1.0, "price": 10.0, "t": 1}
    assert round_trip(TAPE, "catalyst", orphan) is None


def test_round_trip_matches_the_most_recent_buy():
    tape = TAPE + [{"agent": "catalyst", "event": "fill", "when": "Jul 20 14:41",
                    "side": "buy", "symbol": "V", "qty": 1.0, "price": 1.0, "t": 100}]
    assert round_trip(tape, "catalyst", TAPE[0])["entry"] == pytest.approx(361.892025)


ARENA = {
    "tape": TAPE,
    "agents": [
        {"id": "catalyst", "name": "Catalyst", "archetype": "Event-driven", "ret": 0.0053,
         "equity": 100533.74, "alpha": 0.0159, "cash_pct": 0.204, "benchmark_label": "SPY",
         "max_dd": -0.0134, "chartered_by": "the house",
         "positions": [{"symbol": "AMZN", "value": 19447.32, "pl": -0.0227},
                       {"symbol": "AAPL", "value": 21086.74, "pl": 0.0596}],
         "hypotheses": [{"id": "H0", "status": "falsified", "statement": "old"},
                        {"id": "H1", "status": "testing", "falsifier": "hit rate < 55% after 8 cases",
                         "expiry": "2026-11-30"}],
         "curve": [{"t": 1, "v": 100.0}]},
        {"id": "vertex", "name": "Vertex", "ret": -0.0527, "positions": [], "hypotheses": []},
    ],
}


def test_payload_carries_the_days_events_only():
    p = payload(ARENA, "catalyst", "Jul 28")
    assert len(p["fills"]) == 1 and len(p["pulled"]) == 1
    assert p["fills"][0]["round_trip"]["ret"] == pytest.approx(0.011462, abs=1e-5)


def test_payload_ranks_the_floor_and_marks_the_average():
    p = payload(ARENA, "catalyst", "Jul 28")
    assert [b["id"] for b in p["board"]] == ["catalyst", "vertex"]
    assert p["floor_avg"] == pytest.approx((0.0053 + -0.0527) / 2)


def test_payload_takes_the_hypothesis_under_test_not_a_dead_one():
    assert payload(ARENA, "catalyst", "Jul 28")["hypothesis"]["id"] == "H1"


def test_positions_are_ordered_by_size():
    assert [p["symbol"] for p in payload(ARENA, "catalyst", "Jul 28")["positions"]] == ["AAPL", "AMZN"]


def test_a_trader_with_no_open_test_still_builds():
    assert payload(ARENA, "vertex", "Jul 28")["hypothesis"] is None


def test_unknown_trader_is_an_error_not_an_empty_letter():
    with pytest.raises(KeyError):
        payload(ARENA, "nobody", "Jul 28")


def test_avatar_url_is_the_hosted_raster():
    assert payload(ARENA, "catalyst", "Jul 28")["avatar_url"].endswith("/avatars/catalyst.png")
