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


# ------------------------------------------------------------------ rendering

from jobs.letter import deliver, envelope, render_html, render_text, sparkline, subject  # noqa: E402

PROSE = {
    "preheader": "The beat came. The raise did not.",
    "subject": "I sold Visa. The beat came; the raise did not.",
    "line": "I sold Visa this afternoon, one day after buying it.",
    "why": "My rule P2 says the edge dies with the event.",
    "belief": "I believe drift persists in companies that raise guidance.",
    "beliefTie": "Visa never qualified, which is why I sold.",
}


def _p():
    return payload(ARENA, "catalyst", "Jul 28")


def test_the_letter_prints_the_fill_price_from_the_record():
    assert "366.040115" in render_html(_p(), PROSE)


def test_the_letter_prints_the_computed_return_not_the_prose_claim():
    html = render_html(_p(), PROSE)
    assert "+1.15%" in html and "+1.3%" not in html


def test_rendering_refuses_prose_that_states_a_quantity():
    bad = dict(PROSE, line="I sold Visa at $366.04.")
    with pytest.raises(ProseStatesQuantity):
        render_html(_p(), bad)
    with pytest.raises(ProseStatesQuantity):
        render_text(_p(), bad)


def test_the_subject_obeys_the_same_law():
    with pytest.raises(ProseStatesQuantity):
        subject(_p(), dict(PROSE, subject="Up 1.15% today"))


def test_the_falsifier_reaches_the_letter():
    assert "hit rate < 55% after 8 cases" in render_html(_p(), PROSE)


def test_a_trader_with_no_open_test_omits_the_belief_block():
    html = render_html(payload(ARENA, "vertex", "Jul 28"), PROSE)
    assert "What would prove me wrong" not in html


def test_the_face_is_a_hosted_raster_not_inline_svg():
    html = render_html(_p(), PROSE)
    assert "avatars/catalyst.png" in html and "<svg" not in html


def test_the_sparkline_is_cells_not_an_image():
    """Images are blocked by default in many clients; a vanishing chart is worse."""
    s = sparkline([{"v": 100.0}, {"v": 101.0}, {"v": 99.0}])
    assert "<td" in s and "<img" not in s


def test_the_sparkline_declines_to_draw_a_single_point():
    assert sparkline([{"v": 100.0}]) == ""


def test_plain_text_carries_the_same_numbers():
    t = render_text(_p(), PROSE)
    assert "366.040115" in t and "+1.15%" in t


def test_plain_text_carries_no_markup():
    """A bare '<' is fine and expected — the falsifier reads 'hit rate < 55%'.
    What must not survive is a tag."""
    t = render_text(_p(), PROSE)
    assert "hit rate < 55%" in t
    for tag in ("<div", "<table", "<span", "<td", "style=", "&nbsp;"):
        assert tag not in t


def test_the_simulated_disclaimer_is_not_optional():
    assert "simulated" in render_html(_p(), PROSE).lower()
    assert "simulated" in render_text(_p(), PROSE).lower()


def test_the_letter_stays_far_under_gmails_clip_threshold():
    assert len(render_html(_p(), PROSE)) < 102_000


# ------------------------------------------------------------------- delivery

def test_the_envelope_comes_from_the_trader_and_replies_to_the_house():
    m = envelope(_p(), PROSE, "someone@example.com")
    assert m["from"].startswith("Catalyst · Conviction League <catalyst@conviction-league.com>")
    assert m["to"] == ["someone@example.com"]
    assert m["html"] and m["text"]


def test_one_click_unsubscribe_is_present():
    h = envelope(_p(), PROSE, "someone@example.com")["headers"]
    assert "List-Unsubscribe" in h and h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_delivery_without_a_key_refuses_rather_than_pretending():
    with pytest.raises(RuntimeError, match="refusing to pretend"):
        deliver({"to": ["x@y.z"]}, "")


def test_delivery_posts_the_message_and_never_the_key_in_the_body():
    seen = {}

    def fake_post(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return 200, {"id": "abc"}

    status, out = deliver(envelope(_p(), PROSE, "x@y.z"), "re_test", post=fake_post)
    assert status == 200 and out["id"] == "abc"
    assert seen["headers"]["Authorization"] == "Bearer re_test"
    assert "re_test" not in str(seen["body"])
