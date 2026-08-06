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


# --------------------------------------------------- silence, and the voice pass

from jobs.letter import (  # noqa: E402
    SILENT_FRIDAYS_BEFORE_SPEAKING, facts_for_voice, quiet_weeks, voice_pass,
)

WEEK = 7 * 86400


def test_quiet_weeks_counts_from_the_last_thing_that_happened():
    assert quiet_weeks(TAPE, "catalyst", 300 + 3 * WEEK) == 3
    assert quiet_weeks(TAPE, "catalyst", 300) == 0


def test_a_trader_that_never_acted_is_not_reported_as_silent_forever():
    """No events at all is a newborn, not a lapsed trader."""
    assert quiet_weeks(TAPE, "nobody", 10**9) == 0


def test_silence_stays_quiet_below_the_threshold():
    assert eligible("daily", [], silent_fridays=1)[0] is False


def test_after_enough_silence_the_trader_speaks_anyway():
    send, why = eligible("daily", [], silent_fridays=SILENT_FRIDAYS_BEFORE_SPEAKING)
    assert send is True
    assert "still waiting" in why and "silence is not a status" in why


def test_silence_never_overrides_a_principal_who_turned_letters_off():
    assert eligible("off", [], silent_fridays=99)[0] is False


def test_the_voice_prompt_carries_the_charter_voice_not_an_invented_one():
    p = _p()
    assert p["agent"]["voice"] == ""  # the fixture has no charter block
    facts = facts_for_voice(p)
    assert "sell V" in facts and "H1" in facts


def _blocked(cause, note, symbol="NOW"):
    p = _p()
    p["fills"], p["pulled"], p["armed"] = [], [], []
    p["blocked"] = [{"side": "buy", "symbol": symbol, "notional": 4000.0,
                     "cause": cause, "note": note}]
    return facts_for_voice(p)


def test_an_engine_fault_is_never_narrated_as_the_principals_constitution():
    """The letter the operator forwarded on 2026-08-03. Beacon told its
    principal "The constitution blocked both transactions" over a deadlock and
    a missing environment variable — because the fact pack it wrote from said
    "was REFUSED by your constitution" for every refusal there had ever been,
    and a model writes in the register it is handed."""
    facts = _blocked("engine", "the arena could not complete this — a fault on "
                               "our side, not a decision about your trader")
    assert "constitution" not in facts.lower()
    assert "our fault" in facts


def test_a_real_constitutional_refusal_still_says_so():
    """The other half: when the charter did stop the trade, that is the most
    interesting thing that happened all day and the letter must say it."""
    facts = _blocked("constitution", "single-position cap 20% of equity breached")
    assert "refused by your constitution" in facts


def test_an_unclassified_refusal_claims_no_cause():
    facts = _blocked("unclassified", "the order was not accepted")
    assert "constitution" not in facts.lower()
    assert "was not accepted" in facts


def test_facts_for_voice_never_hands_the_model_a_price():
    """It cannot state a figure it was never shown."""
    facts = facts_for_voice(_p())
    assert "366.04" not in facts and "361.89" not in facts


def test_facts_say_plainly_when_nothing_happened():
    assert "nothing was traded today" in facts_for_voice(payload(ARENA, "vertex", "Jul 28"))


def test_the_voice_pass_refuses_a_model_that_states_a_number():
    bad = lambda _prompt: {"subject": "s", "preheader": "p", "line": "I sold V at 366.04",  # noqa: E731
                           "why": "w", "belief": "b", "beliefTie": ""}
    with pytest.raises(ProseStatesQuantity):
        voice_pass(_p(), call=bad)


def test_the_voice_pass_keeps_only_the_fields_the_letter_uses():
    chatty = lambda _p: {"subject": "s", "preheader": "p", "line": "l", "why": "w",  # noqa: E731
                         "belief": "b", "beliefTie": "", "extra": "ignored", "note": "also ignored"}
    out = voice_pass(_p(), call=chatty)
    assert set(out) == {"subject", "preheader", "line", "why", "belief", "beliefTie"}


def test_the_voice_pass_rejects_a_model_that_returns_nothing_useful():
    with pytest.raises(ProseStatesQuantity):
        voice_pass(_p(), call=lambda _p: "not an object")


# ------------------------------------- keeping figures away from the model

from engine.modelreply import first_json_object  # noqa: E402
from jobs.letter import deprice  # noqa: E402


def test_deprice_strips_prices_from_recorded_prose():
    out = deprice("EPS ($3.32 vs $3.22 est) and revenue ($11.63B vs $11.38B est) beat")
    assert "3.32" not in out and "11.63" not in out


def test_deprice_keeps_the_rule_the_trader_cited():
    """The placeholder must carry no digit of its own, or the stripper eats the
    very thing it protects — observed as 'Per Principle P2' becoming
    'Per Principle —'."""
    out = deprice("Per Principle P2 and Hypothesis H1, the edge dissipated")
    assert "P2" in out and "H1" in out


def test_deprice_handles_dates_and_percentages():
    out = deprice("on July 28, 2026 the hit rate fell below 55% for 8 cases")
    assert not any(c.isdigit() for c in out.replace("P", "").replace("H", ""))


def test_deprice_survives_empty_and_none():
    assert deprice("") == "" and deprice(None) == ""


def test_the_model_is_never_shown_a_figure():
    """The whole prompt, not just the fill line."""
    import re as _re
    facts = facts_for_voice(_p())
    stray = _re.findall(r"(?<![PHC])\b\d+", facts)
    assert stray == [], f"the model can see {stray}"


def test_the_model_is_never_shown_an_em_dash():
    """Whatever the redacted note is written in, the model writes back. When the
    price stripper left an em dash behind every figure, the letters came back
    strewn with them, which is the one punctuation the prompt forbids."""
    assert "—" not in deprice("EPS ($3.32 vs $3.22 est) beat; trimmed 40%")
    assert "—" not in facts_for_voice(_p())


def test_a_position_count_is_words_not_a_number():
    assert "a couple of position" in facts_for_voice(_p())


def test_first_json_object_reads_a_plain_reply():
    assert first_json_object('{"line": "hello"}')["line"] == "hello"


def test_first_json_object_survives_fences_and_a_trailing_sentence():
    """Asked for JSON, a model still sometimes fences it or adds a remark —
    a hard parse error would drop an otherwise fine letter."""
    assert first_json_object('```json\n{"line": "hi"}\n```')["line"] == "hi"
    assert first_json_object('{"line": "hi"}\nHope that helps!')["line"] == "hi"


def test_first_json_object_is_not_fooled_by_a_brace_inside_a_string():
    assert first_json_object('{"line": "a } brace", "why": "w"}')["why"] == "w"


def test_first_json_object_raises_on_a_truncated_reply():
    with pytest.raises(ValueError, match="unterminated"):
        first_json_object('{"line": "cut off here')


def test_first_json_object_raises_when_there_is_no_object():
    with pytest.raises(ValueError, match="no json"):
        first_json_object("I could not write this letter.")


def test_the_voice_pass_asks_again_after_a_bad_reply():
    """Replies are stochastic — one arriving truncated must not lose the letter."""
    calls = []

    def flaky(_prompt):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("unterminated json object in the model reply")
        return {"subject": "s", "preheader": "p", "line": "l", "why": "w",
                "belief": "b", "beliefTie": ""}

    assert voice_pass(_p(), call=flaky)["line"] == "l"
    assert len(calls) == 2


def test_a_model_that_keeps_stating_numbers_loses_the_letter():
    """Asking again is allowed; editing a bad reply into a good one is not."""
    attempts = []

    def stubborn(_prompt):
        attempts.append(1)
        return {"subject": "s", "preheader": "p", "line": "I made 12 per cent",
                "why": "w", "belief": "b", "beliefTie": ""}

    with pytest.raises(ProseStatesQuantity):
        voice_pass(_p(), call=stubborn)
    assert len(attempts) > 1  # it tried again before giving up


# ----------------------------------------------------- the record of the send

from jobs.letter import (  # noqa: E402
    DECISIONS,
    LETTER_INSERT,
    archive_row,
    already_written,
    out_name,
)


# ------------------------- the failure path that took down the whole run
#
# 2026-07-31, twice: Neon killed the connection while voice_pass waited 300s on
# Gemini; main()'s except called keep(..., "failed") to record it; keep()'s own
# except called conn.rollback() on the dead connection, which raised — from
# inside the handler meant to contain the first failure — and every trader
# after that one silently got no letter.

from jobs.letter import Archive  # noqa: E402


class Dead:
    """A connection the host has already terminated. Postgres does not let you
    roll back a socket that is gone."""

    def __init__(self, label="dead"):
        self.label = label
        self.rows = []

    def execute(self, sql, args=None):
        raise ConnectionError("the connection is lost")

    def commit(self):
        raise ConnectionError("the connection is lost")

    def rollback(self):
        raise ConnectionError("the connection is lost")

    def close(self):
        pass


class Live:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def execute(self, sql, args=None):
        self.rows.append(args)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def test_recording_a_failure_on_a_dead_connection_does_not_raise():
    """The exact production crash. keep() is called FROM main()'s except, so
    anything it raises escapes the per-trader guard."""
    fresh = Live()
    a = Archive(Dead(), "Jul 31", "close", lambda: fresh)
    a.keep("ballast", "failed", error="Read timed out")   # must not raise
    assert fresh.rows, "a fresh connection should still have recorded it"


def test_a_dead_connection_is_replaced_so_the_truth_still_lands():
    fresh = Live()
    a = Archive(Dead(), "Jul 31", "close", lambda: fresh)
    a.keep("ballast", "sent", reason="1 event(s) on the record")
    assert a.conn is fresh and fresh.commits == 1


def test_when_even_a_fresh_connection_cannot_be_had_it_says_so_and_returns():
    """The database is gone entirely. Still not the run's problem to die of."""
    def no_connection():
        raise ConnectionError("neon is down")

    a = Archive(Dead(), "Jul 31", "close", no_connection)
    a.keep("ballast", "failed", error="Read timed out")   # must not raise


def test_an_unwritable_row_never_reaches_the_caller():
    """archive_row itself refuses an unknown decision; that ValueError must be
    swallowed here too, or it escapes main()'s except exactly like the last
    one did."""
    a = Archive(Live(), "Jul 31", "close", Live)
    a.keep("ballast", "posted")   # not in DECISIONS — must not raise


def test_resting_before_the_model_call_ends_the_transaction():
    """Neon kills a connection that is idle INSIDE a transaction, and
    voice_pass blocks for up to 300s."""
    live = Live()
    Archive(live, "Jul 31", "close", Live).rest()
    assert live.commits == 1


def test_resting_replaces_a_connection_that_is_already_gone():
    fresh = Live()
    a = Archive(Dead(), "Jul 31", "close", lambda: fresh)
    a.rest()                      # must not raise
    assert a.conn is fresh


def test_a_live_connection_is_kept_rather_than_churned():
    """A constraint violation is not a dead socket. Rolling back is enough, and
    reconnecting on every bad row would be its own bug."""
    class Fussy(Live):
        def __init__(self):
            super().__init__()
            self.n = 0

        def execute(self, sql, args=None):
            self.n += 1
            if self.n == 1:
                raise ValueError("bad row")
            self.rows.append(args)

    fussy = Fussy()
    a = Archive(fussy, "Jul 31", "close", lambda: pytest.fail("must not reconnect"))
    a.keep("ballast", "quiet", reason="nothing happened on the record today")
    assert a.conn is fussy


def test_every_column_has_a_value_to_go_in_it():
    """The insert and the row are written in two places and must not drift."""
    assert LETTER_INSERT.count("%s") == len(
        archive_row("ballast", "Jul 28", "close", "quiet"))


def test_an_unknown_decision_is_refused_rather_than_written():
    with pytest.raises(ValueError):
        archive_row("ballast", "Jul 28", "close", "posted")
    assert set(DECISIONS) == {"sent", "quiet", "refused", "failed", "dry"}


def test_a_quiet_trader_is_recorded_with_its_reason():
    row = archive_row("ballast", "Jul 28", "close", "quiet",
                      reason="nothing happened on the record today")
    assert row[3] == "quiet" and "nothing happened" in row[4]


def test_the_receipt_and_the_size_come_from_the_message():
    row = archive_row("ballast", "Jul 28", "close", "sent", subject="s",
                      owner_uid="uid1", provider_id="re_123", html="<p>hi</p>")
    assert row[7] == "re_123"
    assert row[10] == len("<p>hi</p>")


def test_the_bytes_are_bytes_not_characters():
    """A letter is measured against Gmail's clip threshold, which counts
    bytes — and the house style is full of em dashes."""
    assert archive_row("b", "Jul 28", "close", "sent", html="—")[10] == 3


def test_nothing_written_becomes_null_not_an_empty_string():
    row = archive_row("ballast", "Jul 28", "close", "failed", error="boom")
    assert row[5] is None and row[8] is None and row[10] is None
    assert row[11] == "boom"


def test_the_principals_address_cannot_be_recorded_by_accident():
    """It belongs to them and lives in Firestore. There is no parameter for it,
    and there must not be one."""
    with pytest.raises(TypeError):
        archive_row("ballast", "Jul 28", "close", "sent", to="someone@example.com")


class _Conn:
    def __init__(self, found):
        self.found, self.sql, self.params = found, "", ()

    def execute(self, sql, params):
        self.sql, self.params = sql, params
        return self

    def fetchone(self):
        return {"?column?": 1} if self.found else None


def test_a_letter_already_sent_today_is_not_sent_again():
    """The daily-run's cron backup fires ten minutes behind the primary and
    reaches this job too. Without the guard the principal gets two copies."""
    conn = _Conn(found=True)
    assert already_written(conn, "ballast", "Jul 28", "close") is True
    assert conn.params == ("ballast", "Jul 28", "close")


def test_the_guard_only_counts_letters_that_actually_went():
    conn = _Conn(found=False)
    already_written(conn, "ballast", "Jul 28", "close")
    assert "decision = 'sent'" in conn.sql
    assert "interval '1 day'" in conn.sql  # 'Jul 28' carries no year


def test_a_quiet_row_does_not_block_tomorrows_letter():
    assert already_written(_Conn(found=False), "ballast", "Jul 28", "close") is False


def test_the_written_out_letter_is_named_for_the_day_and_the_trader():
    assert out_name("ballast", "Jul 28") == "jul-28-ballast"
