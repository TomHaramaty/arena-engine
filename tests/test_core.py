from engine import core


def test_fill_prices():
    assert abs(core.buy_fill_price(100) - 100.15) < 1e-9
    assert abs(core.sell_fill_price(100) - 99.85) < 1e-9


def test_bench_index_single():
    bench = {"symbols": ["SPY"], "weights": [1.0], "launch_prices": [748.81]}
    assert core.bench_index(bench, {"SPY": 748.81}) == 100.0
    assert core.bench_index(bench, {"SPY": 786.25}) == round(786.25 / 748.81 * 100, 4)


def test_bench_index_blend():
    bench = {
        "symbols": ["SPY", "BTC-USD"],
        "weights": [0.5, 0.5],
        "launch_prices": [700.0, 60000.0],
    }
    # SPY +10%, BTC -10% → blend flat
    idx = core.bench_index(bench, {"SPY": 770.0, "BTC-USD": 54000.0})
    assert abs(idx - 100.0) < 1e-9


def test_bench_index_missing_price():
    bench = {"symbols": ["SPY"], "weights": [1.0], "launch_prices": [700.0]}
    assert core.bench_index(bench, {}) is None


def test_trailing_stop_updates_high_water_and_triggers():
    params = {"trail_pct": 0.10, "high_water": 557.91}
    # price rises → high-water follows, no trigger
    hw, trig = core.trailing_state(params, 600.0)
    assert hw == 600.0 and not trig
    # price falls 9.9% from new high → still no trigger
    hw2, trig2 = core.trailing_state({"trail_pct": 0.10, "high_water": 600.0}, 540.6)
    assert not trig2 and hw2 == 600.0
    # price falls 10%+ from high → trigger
    hw3, trig3 = core.trailing_state({"trail_pct": 0.10, "high_water": 600.0}, 540.0)
    assert trig3


def test_stop_and_limit_triggers():
    assert core.stop_triggered({"trigger_price": 500}, 499.99)
    assert not core.stop_triggered({"trigger_price": 500}, 500.01)
    assert core.limit_buy_triggered({"limit_price": 90}, 89.5)
    assert not core.limit_buy_triggered({"limit_price": 90}, 91.0)


def test_equity_of():
    positions = [{"symbol": "AMD", "qty": 10}]
    assert core.equity_of(1000.0, positions, {"AMD": 500.0}) == 6000.0


def test_limit_sell_and_stop_buy_triggers():
    # take-profit fires at or above the ask
    assert core.limit_sell_triggered({"limit_price": 120}, 120.0)
    assert core.limit_sell_triggered({"limit_price": 120}, 125.0)
    assert not core.limit_sell_triggered({"limit_price": 120}, 119.99)
    # breakout entry fires at or above the trigger
    assert core.stop_buy_triggered({"trigger_price": 150}, 150.0)
    assert not core.stop_buy_triggered({"trigger_price": 150}, 149.5)


def test_triggered_routes_by_kind_and_side():
    """The same price must mean opposite things to a sell-stop and a buy-stop;
    routing them by (kind, side) is the whole safety of the standing book."""
    p = {"trigger_price": 100}
    assert core.triggered("stop", "sell", p, 99.0)
    assert not core.triggered("stop", "sell", p, 101.0)
    assert core.triggered("stop", "buy", p, 101.0)
    assert not core.triggered("stop", "buy", p, 99.0)
    lim = {"limit_price": 100}
    assert core.triggered("limit", "buy", lim, 99.0)
    assert not core.triggered("limit", "buy", lim, 101.0)
    assert core.triggered("limit", "sell", lim, 101.0)
    assert not core.triggered("limit", "sell", lim, 99.0)
    assert core.triggered("trailing_stop", "sell",
                          {"trail_pct": 0.10, "high_water": 100}, 89.0)
    assert not core.triggered("trailing_stop", "sell",
                              {"trail_pct": 0.10, "high_water": 100}, 95.0)


# ---------- constitutional capacity ----------

def _capacity(requested, **kw):
    base = dict(equity=100_000.0, cash=100_000.0, cap_pct=None, held_value=0.0,
                class_cap_pct=None, class_held_value=0.0,
                cap_label="single-position", class_label="equity")
    base.update(kw)
    return core.buy_capacity(requested, **base)


def test_buy_capacity_clips_to_single_position_cap():
    allowed, reasons = _capacity(50_000, cap_pct=0.20)
    assert allowed == 20_000
    assert reasons == ["single-position cap 20%"]


def test_buy_capacity_counts_what_is_already_held():
    allowed, _ = _capacity(50_000, cap_pct=0.20, held_value=15_000)
    assert allowed == 5_000


def test_buy_capacity_cannot_be_outrun_by_slicing():
    """Caps are measured against equity every time, so three 20% buys of the
    same name do not add up to 60%."""
    held = 0.0
    for _ in range(3):
        allowed, _ = _capacity(20_000, cap_pct=0.20, held_value=held)
        held += allowed
    assert held == 20_000


def test_buy_capacity_clips_to_cash():
    allowed, reasons = _capacity(50_000, cash=8_000)
    assert allowed == 8_000
    assert reasons == ["available cash"]


def test_buy_capacity_class_cap_binds_the_sleeve():
    allowed, reasons = _capacity(50_000, class_cap_pct=0.50,
                                 class_held_value=45_000, class_label="crypto")
    assert allowed == 5_000
    assert reasons == ["crypto cap 50%"]


def test_buy_capacity_never_returns_negative():
    allowed, _ = _capacity(10_000, cap_pct=0.20, held_value=30_000)
    assert allowed == 0.0


def test_class_caps_default_deny_unchartered_markets():
    """An agent whose constitution never mentions crypto or leverage is capped
    at zero there — the arena's defaults must not widen underneath a charter."""
    caps = core.class_caps({"max_single_pct": 0.25})
    assert caps["crypto"] == 0.0
    assert caps["inverse_levered"] == 0.0
    assert caps["equity"] is None and caps["etf"] is None


def test_class_caps_honor_legacy_and_general_spellings():
    assert core.class_caps({"crypto_core_cap_pct": 0.5})["crypto"] == 0.5
    caps = core.class_caps({"class_caps": {"inverse_levered": 0.15, "crypto": 0.3}})
    assert caps["inverse_levered"] == 0.15 and caps["crypto"] == 0.3
    # the general form wins over the legacy one
    caps = core.class_caps({"crypto_core_cap_pct": 0.5,
                            "class_caps": {"crypto": 0.1}})
    assert caps["crypto"] == 0.1


def test_symbol_cap_follows_the_chartered_wording():
    """Wildcat's charter says "max single *equity* position", so its 20% does
    not bind its crypto sleeve; Fury's unqualified "max single position" does."""
    wildcat = {"crypto_core_cap_pct": 0.5, "max_single_equity_pct": 0.2}
    assert core.symbol_cap(wildcat, "equity") == 0.2
    assert core.symbol_cap(wildcat, "etf") == 0.2
    # a leveraged ETF is a listed fund bought outright — still an equity
    # position in the sense the charter meant
    assert core.symbol_cap(wildcat, "inverse_levered") == 0.2
    assert core.symbol_cap(wildcat, "crypto") is None
    fury = {"max_single_pct": 0.35, "crypto_core_cap_pct": 0.35}
    assert core.symbol_cap(fury, "equity") == 0.35
    assert core.symbol_cap(fury, "crypto") == 0.35


# ---------- dormancy: inaction as a reviewable event ----------

def test_idle_streak_counts_back_from_the_latest_session():
    # newest first: no order, no order, no order, then a session that traded
    assert core.idle_streak([False, False, False, True, False]) == 3
    assert core.idle_streak([True, False, False]) == 0
    assert core.idle_streak([]) == 0
    assert core.idle_streak([False, False]) == 2


def test_idle_streak_resets_on_any_order():
    """A single order breaks the streak — an agent that trades once a week is
    patient, not dormant, and must not be charged with inaction."""
    assert core.idle_streak([False, True, False, False, False, False]) == 1


def test_dormancy_threshold_is_three_sessions():
    assert core.DORMANT_SESSIONS == 3
    flags = [False] * core.DORMANT_SESSIONS
    assert core.idle_streak(flags) >= core.DORMANT_SESSIONS
    assert core.idle_streak(flags[:-1]) < core.DORMANT_SESSIONS
