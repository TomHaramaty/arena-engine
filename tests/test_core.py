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
