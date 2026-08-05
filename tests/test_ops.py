"""The disposal layer: what a brain is allowed to do to the book.

"Brains propose, the engine disposes" is the arena's central claim, and
runner/ops.py is where the disposing happens — every constitutional rejection,
every clip, every long-only refusal. It had no tests. These are them.

The contract each test defends is the same one the record makes to a reader: an
operation is accepted, clipped-and-recorded, or rejected with a reason, and
nothing an agent can write in its output moves the book any other way.
"""
import json

import pytest

from engine import core, marketdata
from runner import ops
from tests.fakedb import FakeConn, agent

RUN = 42


def block(*operations):
    """A brain's output as the runner really receives it — prose, then a fence."""
    return ("Here is my reasoning about the session.\n\n```json\n"
            + json.dumps({"operations": list(operations)})
            + "\n```\n")


def journal(title="a day"):
    return {"type": "journal_entry", "title": title, "body_markdown": "body"}


def buy(symbol="AMD", notional=10_000, **kw):
    op = {"type": "place_order", "side": "buy", "symbol": symbol,
          "notional_usd": notional, "thesis": "it goes up",
          "invalidation": "it does not", "review_by": "2026-08-15"}
    op.update(kw)
    return op


def conn(**kw):
    kw.setdefault("watchlist", {"AMD": "equity", "SPY": "etf",
                                "BTC-USD": "crypto", "TQQQ": "inverse_levered"})
    kw.setdefault("ticks", {"AMD": 100.0, "SPY": 500.0, "BTC-USD": 60_000.0,
                            "TQQQ": 50.0})
    return FakeConn(**kw)


def apply(c, a, *operations, dry=False, resolver=None):
    return ops.validate_and_apply(c, a, RUN, list(operations), dry=dry,
                                  resolver=resolver)


def verdicts(results):
    return [(o.get("type"), v) for o, v, _ in results]


def only(results):
    assert len(results) == 1
    return results[0]


# ---------- parse: what counts as an operations block ----------

def test_parse_requires_a_fenced_block():
    with pytest.raises(ops.OpsParseError):
        ops.parse("I have decided to do nothing today.")


def test_parse_takes_the_last_block_so_an_example_never_executes():
    """A brain that illustrates a trade mid-argument must not have the
    illustration applied. The operative block is the last one."""
    text = (block(journal(), buy(notional=99_000))
            + "\nOn reflection, smaller.\n"
            + block(journal(), buy(notional=1_000)))
    parsed = ops.parse(text)
    assert parsed[1]["notional_usd"] == 1_000


def test_parse_rejects_missing_or_empty_operations():
    with pytest.raises(ops.OpsParseError):
        ops.parse('```json\n{"operations": []}\n```')
    with pytest.raises(ops.OpsParseError):
        ops.parse('```json\n{"notes": "none"}\n```')


def test_parse_demands_exactly_one_journal_entry():
    """Every session leaves exactly one entry: none is an unexplained trade,
    two is an unauditable record."""
    with pytest.raises(ops.OpsParseError):
        ops.parse(block(buy()))
    with pytest.raises(ops.OpsParseError):
        ops.parse(block(journal(), journal("and again")))
    assert len(ops.parse(block(journal(), buy()))) == 2


# ---------- every op is recorded, whatever the verdict ----------

def test_every_operation_is_recorded_with_its_verdict():
    c, a = conn(), agent(max_single_pct=0.25)
    apply(c, a, journal(), buy(), {"type": "nonsense_op"})
    assert [(t, v) for t, v, _ in c.operations] == [
        ("journal_entry", "accepted"),
        ("place_order", "accepted"),
        ("nonsense_op", "rejected"),
    ]


def test_unknown_op_type_is_rejected_by_name():
    c = conn()
    _, verdict, reason = only(apply(c, agent(), {"type": "liquidate_everything"}))
    assert verdict == "rejected" and "liquidate_everything" in reason


def test_one_bad_operation_never_poisons_the_batch():
    """A malformed op must cost its author that op, not the whole session."""
    c, a = conn(), agent(max_single_pct=0.25)
    results = apply(c, a, journal(), buy(notional="not a number"), buy(notional=5_000))
    assert verdicts(results) == [
        ("journal_entry", "accepted"),
        ("place_order", "rejected"),
        ("place_order", "accepted"),
    ]
    assert c.position_qty("AMD") > 0


def test_dry_run_applies_nothing_and_records_nothing():
    c, a = conn(), agent(max_single_pct=0.25)
    results = apply(c, a, journal(), buy(), dry=True)
    assert verdicts(results)[1] == ("place_order", "accepted")
    assert c.cash == 100_000.0 and not c.positions and not c.operations


# ---------- buys: the constitutional maximum, never more ----------

def test_buy_moves_cash_position_order_and_fill_together():
    c, a = conn(), agent(max_single_pct=0.25)
    _, verdict, reason = apply(c, a, journal(), buy(notional=10_000))[1]
    assert verdict == "accepted"
    fill = core.buy_fill_price(100.0)
    assert c.cash == pytest.approx(90_000.0)
    assert c.position_qty("AMD") == pytest.approx(10_000 / fill)
    assert c.positions["AMD"]["avg_fill"] == pytest.approx(fill)
    assert len(c.wrote("insert into orders")) == 1
    assert len(c.wrote("insert into fills")) == 1
    assert f"{fill:.2f}" in reason


def test_buy_pays_the_cost_against_itself():
    """Slippage is charged to the agent: 0.15% worse than the mark, every time."""
    c, a = conn(), agent(max_single_pct=1.0)
    apply(c, a, journal(), buy(notional=10_000))
    assert c.positions["AMD"]["avg_fill"] > 100.0
    assert c.position_qty("AMD") < 100.0


def test_oversized_buy_is_clipped_and_the_clip_is_recorded():
    """The engine executes the constitutional maximum of the intent — it never
    silently voids a proposal, and never silently oversizes one either."""
    c, a = conn(), agent(max_single_pct=0.20)
    _, verdict, reason = apply(c, a, journal(), buy(notional=50_000))[1]
    assert verdict == "accepted"
    assert "clipped from $50,000 to $20,000" in reason
    assert "single-position cap 20%" in reason
    assert c.cash == pytest.approx(80_000.0)


def test_a_buy_cannot_outrun_its_cap_by_slicing_inside_one_batch():
    """The integrity property: capacity is read fresh per operation, so three
    20% buys of the same name are still one 20% position."""
    c, a = conn(), agent(max_single_pct=0.20)
    results = apply(c, a, journal(), buy(notional=20_000), buy(notional=20_000),
                    buy(notional=20_000))
    held = c.position_qty("AMD") * c.ticks["AMD"]
    assert held <= 0.20 * 100_000 + 1.0
    # the later slices are refused for what they are, not silently dropped
    assert [v for _, v, _ in results[1:]] == ["accepted", "rejected", "rejected"]


def test_unchartered_market_is_capped_at_zero():
    """An agent whose constitution never mentions crypto cannot buy crypto —
    the arena's defaults must not widen underneath a charter."""
    c, a = conn(), agent(max_single_pct=0.25)
    _, verdict, reason = only(apply(c, a, buy(symbol="BTC-USD", notional=10_000)))
    assert verdict == "rejected"
    assert "crypto cap 0%" in reason and c.cash == 100_000.0


def test_chartered_crypto_sleeve_binds_at_its_own_ceiling():
    c, a = conn(), agent(max_single_pct=0.5, class_caps={"crypto": 0.10})
    _, verdict, reason = only(apply(c, a, buy(symbol="BTC-USD", notional=50_000)))
    assert verdict == "accepted" and "crypto cap 10%" in reason
    assert c.cash == pytest.approx(90_000.0)


def test_buy_is_clipped_to_cash_not_to_optimism():
    # No single-position cap chartered, so cash is the only ceiling left.
    c, a = conn(cash=3_000.0), agent()
    _, verdict, reason = only(apply(c, a, buy(notional=50_000)))
    assert verdict == "accepted" and "available cash" in reason
    assert c.cash == pytest.approx(0.0)


def test_buy_with_no_meaningful_capacity_is_rejected_not_dust_filled():
    """Below $500 of room the engine refuses rather than opening a token
    position that costs a slot and proves nothing."""
    c, a = conn(cash=200.0), agent(max_single_pct=1.0)
    _, verdict, reason = only(apply(c, a, buy(notional=10_000)))
    assert verdict == "rejected" and "no meaningful capacity" in reason
    assert not c.positions


def test_buy_needs_thesis_invalidation_and_review_by():
    """A position with no stated way to be wrong is not a position, it is a
    hope — the review date is what makes the reflection able to judge it."""
    c, a = conn(), agent(max_single_pct=0.25)
    for missing in ("thesis", "invalidation", "review_by"):
        op = buy()
        del op[missing]
        _, verdict, reason = only(apply(conn(), a, op))
        assert verdict == "rejected"
        assert "thesis + invalidation + review_by" in reason


def test_buy_needs_a_positive_notional():
    c, a = conn(), agent(max_single_pct=0.25)
    for bad in (0, -5_000, None):
        _, verdict, reason = only(apply(conn(), a, buy(notional=bad)))
        assert verdict == "rejected" and "positive notional_usd" in reason


def test_symbol_off_the_watchlist_is_rejected_with_the_way_forward():
    c, a = conn(), agent(max_single_pct=0.25)
    _, verdict, reason = only(apply(c, a, buy(symbol="NVDA")))
    assert verdict == "rejected"
    assert "not on watchlist" in reason and "watchlist_request" in reason


def test_symbol_with_no_engine_price_is_rejected_never_guessed():
    c, a = conn(ticks={}), agent(max_single_pct=0.25)
    _, verdict, reason = only(apply(c, a, buy()))
    assert verdict == "rejected" and "no engine price" in reason


def test_side_must_be_buy_or_sell():
    c = conn()
    _, verdict, reason = only(apply(c, agent(), {
        "type": "place_order", "side": "short", "symbol": "AMD"}))
    assert verdict == "rejected" and "buy or sell" in reason


# ---------- sells: long-only, and no selling what you do not hold ----------

def test_sell_without_a_position_is_refused_as_a_short():
    c, a = conn(), agent()
    _, verdict, reason = only(apply(c, a, {
        "type": "place_order", "side": "sell", "symbol": "AMD", "qty": 10}))
    assert verdict == "rejected"
    assert "no AMD position" in reason and "no shorts" in reason


def test_sell_beyond_the_position_is_refused():
    c = conn(positions={"AMD": {"qty": 10.0, "avg_fill": 90.0}})
    _, verdict, reason = only(apply(c, agent(), {
        "type": "place_order", "side": "sell", "symbol": "AMD", "qty": 25}))
    assert verdict == "rejected" and "exceeds position" in reason
    assert c.position_qty("AMD") == 10.0


def test_full_sell_closes_the_position_and_files_the_post_mortem():
    """Closing a position is what makes a reflection due — the trigger is filed
    handled, because it schedules the post-mortem rather than waking the brain
    that has just acted."""
    c = conn(positions={"AMD": {"qty": 10.0, "avg_fill": 90.0}})
    _, verdict, _ = only(apply(c, agent(), {
        "type": "place_order", "side": "sell", "symbol": "AMD", "qty": "all"}))
    assert verdict == "accepted"
    assert "AMD" not in c.positions
    assert c.cash == pytest.approx(100_000.0 + 10 * core.sell_fill_price(100.0))
    closed = [t for t in c.triggers if t["kind"] == "position_closed"]
    assert len(closed) == 1 and closed[0]["handled"] is True


def test_partial_sell_leaves_the_position_open_and_files_nothing():
    c = conn(positions={"AMD": {"qty": 10.0, "avg_fill": 90.0}})
    only(apply(c, agent(), {"type": "place_order", "side": "sell",
                            "symbol": "AMD", "qty": 4}))
    assert c.position_qty("AMD") == pytest.approx(6.0)
    assert not [t for t in c.triggers if t["kind"] == "position_closed"]


def test_sell_with_no_qty_means_the_whole_position():
    c = conn(positions={"AMD": {"qty": 7.0, "avg_fill": 90.0}})
    only(apply(c, agent(), {"type": "place_order", "side": "sell",
                            "symbol": "AMD"}))
    assert "AMD" not in c.positions


# ---------- the standing book ----------

def stand(**kw):
    op = {"type": "register_standing_order", "symbol": "AMD", "kind": "stop",
          "trigger_price": 90.0}
    op.update(kw)
    return op


def test_standing_order_kind_is_closed_vocabulary():
    _, verdict, reason = only(apply(conn(), agent(), stand(kind="hunch")))
    assert verdict == "rejected" and "stop|trailing_stop|limit" in reason


def test_stop_needs_a_trigger_and_limit_needs_a_price():
    _, v1, r1 = only(apply(conn(), agent(), stand(trigger_price=None)))
    assert v1 == "rejected" and "trigger_price" in r1
    _, v2, r2 = only(apply(conn(), agent(), stand(kind="limit", limit_price=None)))
    assert v2 == "rejected" and "limit_price" in r2


def test_trailing_stop_is_sell_side_protection_only():
    c = conn(positions={"AMD": {"qty": 5.0, "avg_fill": 90.0}})
    _, verdict, reason = only(apply(c, agent(), stand(
        kind="trailing_stop", side="buy", trail_pct=0.1)))
    assert verdict == "rejected" and "sell-side protection" in reason


def test_trailing_stop_seeds_its_high_water_from_the_engine_price():
    c = conn(positions={"AMD": {"qty": 5.0, "avg_fill": 90.0}})
    only(apply(c, agent(), stand(kind="trailing_stop", trail_pct=0.10)))
    params = json.loads(c.wrote("insert into orders")[0][1][5])
    assert params == {"trail_pct": 0.10, "high_water": 100.0}


def test_a_resting_sell_needs_something_to_sell():
    _, verdict, reason = only(apply(conn(), agent(), stand()))
    assert verdict == "rejected" and "no position to sell" in reason


def test_a_resting_buy_must_say_how_much():
    _, verdict, reason = only(apply(conn(), agent(), stand(
        kind="limit", side="buy", limit_price=90.0)))
    assert verdict == "rejected" and "needs notional_usd" in reason


def test_an_order_already_through_the_market_says_so():
    """Accepted, because it is a legitimate market order with extra steps — but
    the agent is told, so it cannot claim it was resting patiently."""
    c = conn(positions={"AMD": {"qty": 5.0, "avg_fill": 90.0}})
    _, verdict, reason = only(apply(c, agent(), stand(trigger_price=120.0)))
    assert verdict == "accepted" and "already through the market" in reason


def test_a_resting_buy_capacity_is_not_judged_until_it_triggers():
    """Registration is not execution: a buy far larger than today's cash is
    allowed to rest, and gets clipped at the trigger instead."""
    c = conn(cash=1_000.0)
    _, verdict, _ = only(apply(c, agent(max_single_pct=0.2), stand(
        kind="limit", side="buy", limit_price=50.0, notional_usd=90_000)))
    assert verdict == "accepted"


# ---------- cancels: only your own, only what is open ----------

def test_cancel_refuses_another_agents_order():
    c = conn(orders=[{"id": 5, "agent_id": "wildcat", "status": "open"}])
    _, verdict, reason = only(apply(c, agent("tempo"),
                                    {"type": "cancel_order", "order_id": 5}))
    assert verdict == "rejected" and "no open order 5 for you" in reason
    assert c.orders[0]["status"] == "open"


def test_cancel_closes_your_own_open_order_with_your_note():
    c = conn(orders=[{"id": 5, "agent_id": "tempo", "status": "open"}])
    _, verdict, _ = only(apply(c, agent("tempo"), {
        "type": "cancel_order", "order_id": 5, "note": "thesis is dead"}))
    assert verdict == "accepted"
    assert c.orders[0]["status"] == "canceled"
    assert c.orders[0]["reason"] == "thesis is dead"


# ---------- guidance: standing, and no authority ----------

def g(cid="C1", **kw):
    op = {"type": "guidance_response", "cid": cid, "disposition": "declined",
          "note": "I read it and I am not changing the rule, because the "
                  "evidence still points the other way."}
    op.update(kw)
    return op


def waiting(cid="C1"):
    return [{"id": 1, "agent_id": "tempo", "cid": cid, "disposition": None}]


def test_guidance_answer_must_address_a_note_that_is_waiting():
    c = conn(guidance=waiting("C1"))
    _, verdict, reason = only(apply(c, agent("tempo"), g(cid="C9")))
    assert verdict == "rejected" and "no unanswered guidance C9" in reason


def test_guidance_disposition_is_closed_vocabulary():
    c = conn(guidance=waiting())
    _, verdict, reason = only(apply(c, agent("tempo"), g(disposition="noted")))
    assert verdict == "rejected"
    assert "adopted, converted, declined or refused" in reason


def test_guidance_answer_must_be_in_the_agents_own_words():
    c = conn(guidance=waiting())
    _, verdict, reason = only(apply(c, agent("tempo"), g(note="declined")))
    assert verdict == "rejected" and "not a label" in reason


def test_converted_means_the_test_was_actually_filed():
    """"I turned your note into a hypothesis" is only true if the hypothesis is
    in the same block."""
    c = conn(guidance=waiting())
    _, verdict, reason = only(apply(c, agent("tempo"), g(disposition="converted")))
    assert verdict == "rejected" and "in this same operations block" in reason

    c = conn(guidance=waiting())
    results = apply(c, agent("tempo"), g(disposition="converted"),
                    {"type": "hypothesis_op", "op": "propose"})
    assert [v for _, v, _ in results] == ["accepted", "accepted"]
    assert c.guidance[0]["disposition"] == "converted"


def test_an_answered_note_carries_the_agents_words_into_the_record():
    c = conn(guidance=waiting())
    op = g()
    _, verdict, reason = only(apply(c, agent("tempo"), op))
    assert verdict == "accepted" and reason == "C1 answered: declined"
    assert c.guidance[0]["answer"] == op["note"]


def test_a_lowercase_cid_still_finds_its_note():
    c = conn(guidance=waiting("C1"))
    _, verdict, _ = only(apply(c, agent("tempo"), g(cid=" c1 ")))
    assert verdict == "accepted"


# ---------- watchlist: a grant that cannot quote is worse than a refusal ----------

def resolves(symbol):
    return {"source_symbol": "BINANCE:SOLUSDT", "asset_class": "crypto",
            "description": "SOL spot",
            "quote": {"price": 150.0, "prev_close": 149.0, "high": None,
                      "low": None, "open": None, "ts": None}}


def test_watchlist_request_validates_the_symbol_shape():
    _, verdict, reason = only(apply(conn(), agent(), {
        "type": "watchlist_request", "symbol": "not a ticker!"}))
    assert verdict == "rejected" and "invalid symbol" in reason


def test_watchlist_request_for_something_already_listed_is_a_no_op():
    c = conn()
    _, verdict, reason = only(apply(c, agent(), {
        "type": "watchlist_request", "symbol": "AMD"}))
    assert verdict == "accepted" and reason == "already on watchlist"
    assert not c.wrote("insert into watchlist")


def test_an_unreachable_data_source_is_not_a_verdict_on_the_symbol():
    """The engine may not tell an agent its instrument does not exist because a
    lookup was rate-limited. That would be the engine inventing a fact."""
    def unreachable(symbol):
        raise marketdata.QuoteError("429 too many requests")

    c = conn()
    _, verdict, reason = only(apply(c, agent(), {
        "type": "watchlist_request", "symbol": "SOL-USD"}, resolver=unreachable))
    assert verdict == "rejected"
    assert "not a verdict on the symbol" in reason and "request it again" in reason
    assert not c.wrote("insert into watchlist")


def test_an_unquotable_symbol_is_refused_and_says_what_the_arena_prices():
    c = conn()
    _, verdict, reason = only(apply(c, agent(), {
        "type": "watchlist_request", "symbol": "EURUSD"},
        resolver=lambda s: None))
    assert verdict == "rejected" and "does not resolve" in reason


def test_a_granted_symbol_is_tradable_in_the_same_run():
    """A grant an agent cannot act on for another six hours is a grant no brain
    spends an operation on — so the resolving quote is seeded as a tick."""
    c = conn()
    _, verdict, reason = only(apply(c, agent(), {
        "type": "watchlist_request", "symbol": "SOL-USD"}, resolver=resolves))
    assert verdict == "accepted" and "tradable now" in reason
    assert c.watchlist["SOL-USD"] == "crypto"
    assert c.wrote("insert into ticks")
    granted = [t for t in c.triggers if t["kind"] == "watchlist_granted"]
    assert len(granted) == 1 and granted[0]["handled"] is True


# ---------- a database error mid-batch must not take the session with it ----------

def poisoned(fail_on, **kw):
    """A connection that deadlocks on one statement and then refuses everything,
    the way Postgres really does."""
    kw.setdefault("watchlist", {"AMD": "equity", "SPY": "etf"})
    kw.setdefault("ticks", {"AMD": 100.0, "SPY": 500.0})
    return FakeConn(fail_on=fail_on, **kw)


def test_a_deadlock_mid_op_is_recorded_as_a_rejection():
    """The failure this closes crashed vector's first bell on 2026-07-29.

    A DB error inside an op leaves the transaction aborted, so `record()`'s own
    insert used to raise InFailedSqlTransaction — a second exception, thrown from
    inside the first one's handler, escaping validate_and_apply entirely. The
    session died and every op accepted earlier in the batch rolled back with it.
    """
    c = poisoned("insert into fills")
    results = apply(c, agent(max_single_pct=0.25), journal(), buy())
    assert [v for _, v, _ in results] == ["accepted", "rejected"]
    assert "deadlock" in results[1][2]


def test_the_ops_after_a_deadlock_still_get_their_session():
    """One bad op costs its author that op — a promise the old code could not
    keep once the failure was a database error rather than a bad payload."""
    c = poisoned("insert into fills")          # only the first buy hits it
    results = apply(c, agent(max_single_pct=0.5), journal(),
                    buy(symbol="AMD"), buy(symbol="SPY", notional=5_000))
    assert [v for _, v, _ in results] == ["accepted", "rejected", "accepted"]
    assert c.position_qty("SPY") > 0


def test_a_deadlock_does_not_leave_the_failed_op_half_applied():
    """Rolling back to the savepoint undoes whatever the op wrote before it
    failed, so an op lands whole or not at all."""
    c = poisoned("insert into fills")
    apply(c, agent(max_single_pct=0.25), journal(), buy(notional=10_000))
    assert c.aborted is False
    assert c.savepoints == []                  # every savepoint released


def test_the_journal_still_survives_a_deadlocked_trade():
    """jobs/agent_run reads the accepted journal_entry out of these results to
    write the record. If the batch escapes, there is no entry and the run dies
    with nothing filed."""
    c = poisoned("insert into fills")
    results = apply(c, agent(max_single_pct=0.25), journal("the deadlocked day"),
                    buy())
    entry = next(o for o, v, _ in results
                 if o.get("type") == "journal_entry" and v == "accepted")
    assert entry["title"] == "the deadlocked day"


def test_a_grant_lifts_a_symbol_that_was_parked_under_another_status():
    """`on conflict do nothing` met the seven rows parked in 'pending_engine'
    on 2026-07-27 and left every one of them exactly as it found it, while this
    handler went on recording "granted — tradable now, this run". The grant is
    the row being active."""
    c = conn(parked={"SOL-USD"})
    _, verdict, reason = only(apply(c, agent(), {
        "type": "watchlist_request", "symbol": "SOL-USD"}, resolver=resolves))
    assert verdict == "accepted" and "tradable now" in reason
    assert "SOL-USD" not in c.parked and c.watchlist["SOL-USD"] == "crypto"


def test_a_refused_request_is_named_by_the_order_it_refuses():
    """11 of the 21 refusals in the record to 2026-08-04 told an agent to file
    a watchlist_request it had filed three lines earlier, in the same block."""
    def unreachable(symbol):
        raise marketdata.QuoteError("data source unreachable")

    c = conn()
    results = apply(c, agent(), journal(),
                    {"type": "watchlist_request", "symbol": "LMT"},
                    buy(symbol="LMT", notional=4_000),
                    resolver=unreachable)
    reason = results[2][2]
    assert "file watchlist_request first" not in reason
    assert "in this same block was refused" in reason
    assert "data source unreachable" in reason


def test_an_order_for_a_symbol_never_asked_for_still_says_how_to_ask():
    """The generic message is right when it IS the truth — most agents that
    hit it simply never filed the request."""
    _, _, reason = only(apply(conn(), agent(), buy(symbol="LMT")))
    assert "file watchlist_request first" in reason


# ---------- a commit mid-batch must not take the session with it ----------
#
# The 2026-08-03/04 outage: engine.core.insert_ticks committed, the watchlist
# grant calls it from inside the per-operation savepoint, and a commit destroys
# every savepoint in the transaction it ends. `RELEASE SAVEPOINT op` then raised
# from a `finally`, escaping the batch. 30 sessions died — every one of them a
# run that had just been granted the symbol it asked for. The grant survived
# (it had been committed); the audit rows for it did not, and every operation
# after it never ran.


def test_seeding_the_grants_quote_does_not_commit():
    """The fix at its source: a low-level write helper does not end a
    transaction it did not begin."""
    c = conn()
    apply(c, agent(), {"type": "watchlist_request", "symbol": "SOL-USD"},
          resolver=resolves)
    assert c.commits == 1                      # the batch's own commit, and no other


def test_a_grant_does_not_cost_the_session_its_remaining_operations():
    """The production symptom, stated as a contract: the operations after a
    grant are the whole reason the grant was asked for."""
    c = conn()
    a = agent(max_single_pct=0.5, class_caps={"crypto": 0.5})
    results = apply(c, a, journal(),
                    {"type": "watchlist_request", "symbol": "SOL-USD"},
                    buy(symbol="SOL-USD", notional=10_000),
                    resolver=resolves)
    assert [v for _, v, _ in results] == ["accepted", "accepted", "accepted"]
    assert c.position_qty("SOL-USD") > 0


def test_a_handler_that_commits_is_an_anomaly_not_a_dead_session():
    """The backstop. The commit is fixed where it lived, but the savepoint is a
    net for ONE operation and losing the net must never cost the session — so
    the next stray commit is a logged rejection, not 30 dead runs."""
    c = conn()

    def commits_midway(symbol):
        c.commit()                             # what insert_ticks used to do
        raise marketdata.QuoteError("boom")

    results = apply(c, agent(max_single_pct=0.5), journal(),
                    {"type": "watchlist_request", "symbol": "SOL-USD"},
                    buy(symbol="AMD", notional=10_000),
                    resolver=commits_midway)
    assert [v for _, v, _ in results] == ["accepted", "rejected", "accepted"]
    assert c.position_qty("AMD") > 0           # the batch carried on
    assert c.savepoints == []


def test_a_granted_symbol_still_answers_to_the_class_caps():
    """Being on the watchlist is permission to price something, never
    permission to hold it — the charter still decides."""
    c = conn()
    apply(c, agent(max_single_pct=0.25),
          {"type": "watchlist_request", "symbol": "SOL-USD"}, resolver=resolves)
    c.ticks["SOL-USD"] = 150.0
    _, verdict, reason = only(apply(c, agent(max_single_pct=0.25),
                                    buy(symbol="SOL-USD", notional=10_000)))
    assert verdict == "rejected" and "crypto cap 0%" in reason


# --------------------------------------------------- reading the operations
#
# The parser used to read only the LAST fenced json block. A real session on
# 2026-07-31 emitted a second fence after its operations and died on a
# JSONDecodeError with the operations sitting intact in the block above.

JOURNAL = '{"operations": [{"type": "journal_entry", "title": "t", "body_markdown": "b"}]}'


def test_it_reads_the_operations_block():
    assert ops.parse(f"words\n```json\n{JOURNAL}\n```\n")[0]["type"] == "journal_entry"


def test_a_later_broken_fence_does_not_bury_the_operations():
    """The production failure, as a test."""
    text = f"```json\n{JOURNAL}\n```\nand then\n```json\n{{\"note\": ...}}\n```\n"
    assert ops.parse(text)[0]["type"] == "journal_entry"


def test_a_later_valid_block_without_operations_is_skipped():
    text = f"```json\n{JOURNAL}\n```\n```json\n{{\"summary\": \"done\"}}\n```"
    assert ops.parse(text)[0]["type"] == "journal_entry"


def test_the_newest_operations_block_wins():
    """Two real op blocks means the session changed its mind; the last word
    stands, exactly as before."""
    first = '{"operations": [{"type": "journal_entry", "title": "old", "body_markdown": "b"}]}'
    text = f"```json\n{first}\n```\n```json\n{JOURNAL}\n```"
    assert ops.parse(text)[0]["title"] == "t"


def test_a_single_malformed_block_still_fails_the_session():
    """Tolerating shapes is not the same as guessing at content: a block that
    looks like operations but does not parse fails the session, and says so."""
    with pytest.raises(ops.OpsParseError, match="not valid json"):
        ops.parse('```json\n{"operations": [oops]}\n```')


def test_a_truncated_block_never_even_looks_like_one():
    """No closing brace, no match: the session fails as it should."""
    with pytest.raises(ops.OpsParseError, match="no fenced json"):
        ops.parse('```json\n{"operations": [\n```')


def test_no_block_at_all_still_fails():
    with pytest.raises(ops.OpsParseError, match="no fenced json"):
        ops.parse("I have decided to hold.")
