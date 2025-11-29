#!/usr/bin/env python3
"""
Comprehensive test suite for oracle_orderbook.py v3.3
Run: python3 test_orderbook_full.py [--no-live] [--no-perf] [--quick]
"""

import sys, time, random, threading, traceback, tempfile, os
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path

from oracle_orderbook import (
    OrderBookSnapshot, OrderBookLevel, OrderBookAnalyzer, OrderBookManager, 
    BinanceMultiStream, BinanceOrderBookStream, BacktestOrderBookProvider, 
    SyntheticOrderBook, DataValidator, AlertManager, StateManager, 
    ThrottledCallback, OrderBookDeltaHandler, TickClassifier, StreamHealth, 
    TradeSide, OrderBookConstants, ALL_TRADING_SYMBOLS, HAS_WEBSOCKETS, _now_ms
)

class TestResults:
    def __init__(self):
        self.passed, self.failed, self.errors, self.timings = 0, 0, [], {}
    
    def record(self, name: str, success: bool, duration: float, error: str = "") -> None:
        if success:
            self.passed += 1
        else:
            self.failed += 1
            if error:
                self.errors.append(f"{name}: {error}")
        self.timings[name] = duration
    
    def summary(self) -> str:
        total = self.passed + self.failed
        return f"\n{'='*70}\n TEST RESULTS: {self.passed}/{total} passed ({100*self.passed/max(1,total):.1f}%)\n{'='*70}\n"

results = TestResults()

def run_test(name: str, test_fn, *args, **kwargs) -> bool:
    print(f"\n[TEST] {name}...")
    start = time.time()
    try:
        success = test_fn(*args, **kwargs)
        duration = time.time() - start
        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"   {status} ({duration:.2f}s)")
        results.record(name, success, duration)
        return success
    except Exception as e:
        duration = time.time() - start
        print(f"   ❌ ERROR: {e}")
        traceback.print_exc()
        results.record(name, False, duration, str(e))
        return False

# ============================================================
# UNIT TESTS
# ============================================================

def test_order_book_level() -> bool:
    level = OrderBookLevel(100.0, 10.0)
    assert level.price == 100.0 and level.size == 10.0 and level.value == 1000.0 and level.to_tuple() == (100.0, 10.0)
    level2 = OrderBookLevel("99.5", "5.5")
    assert level2.price == 99.5 and level2.size == 5.5
    try:
        OrderBookLevel(-1, 10)
        return False
    except ValueError:
        pass
    try:
        OrderBookLevel(100, -1)
        return False
    except ValueError:
        pass
    return True

def test_order_book_snapshot_creation() -> bool:
    snap1 = OrderBookSnapshot.from_raw(symbol="BTC/USDT", bids=[(100, 10), (99, 20), (98, 30)], asks=[(101, 15), (102, 25), (103, 35)], timestamp=_now_ms(), exchange="test")
    assert snap1.is_valid and not snap1.is_crossed and snap1.best_bid == 100 and snap1.best_ask == 101 and snap1.symbol == "BTC/USDT"
    bids = [OrderBookLevel(100, 10), OrderBookLevel(99, 20)]
    asks = [OrderBookLevel(101, 15), OrderBookLevel(102, 25)]
    snap2 = OrderBookSnapshot.from_sorted(symbol="ETH/USDT", bids=bids, asks=asks)
    assert snap2.is_valid and snap2.best_bid == 100
    data = snap1.to_dict()
    snap3 = OrderBookSnapshot.from_dict(data)
    assert snap3.symbol == snap1.symbol and snap3.best_bid == snap1.best_bid
    return True

def test_order_book_metrics() -> bool:
    snap = OrderBookSnapshot.from_raw(symbol="TEST/USDT", bids=[(100, 50), (99, 30), (98, 20)], asks=[(101, 25), (102, 35), (103, 40)])
    assert snap.mid_price == 100.5 and snap.spread == 1.0 and abs(snap.spread_pct - 0.995) < 0.01 and abs(snap.spread_bps - 99.5) < 1
    assert abs(snap.imbalance - 0.333) < 0.01
    bid_vol, ask_vol = 50 + 30 + 20, 25 + 35 + 40
    assert abs(snap.depth_imbalance(3)) < 0.01
    vwap_bid = (100*50 + 99*30 + 98*20) / 100
    assert abs(snap.vwap_bid(3) - vwap_bid) < 0.01
    micro = (100 * 25 + 101 * 50) / 75
    assert abs(snap.microprice() - micro) < 0.01
    return True

def test_market_impact() -> bool:
    snap = OrderBookSnapshot.from_raw(symbol="TEST/USDT", bids=[(100, 10), (99, 10), (98, 10)], asks=[(101, 10), (102, 10), (103, 10)])
    assert snap.market_impact(5, "buy") == 101.0
    expected = (10 * 101 + 5 * 102) / 15
    assert abs(snap.market_impact(15, "buy") - expected) < 0.01
    expected_sell = (10 * 100 + 5 * 99) / 15
    assert abs(snap.market_impact(15, "sell") - expected_sell) < 0.01
    return True

def test_wall_detection() -> bool:
    snap = OrderBookSnapshot.from_raw(symbol="TEST/USDT", bids=[(100, 10), (99, 100), (98, 10), (97, 10)], asks=[(101, 10), (102, 10), (103, 150), (104, 10)])
    walls = snap.find_walls(threshold_mult=3.0)
    assert len(walls["bid_walls"]) >= 1 and walls["bid_walls"][0].price == 99
    assert any(w.price == 103 for w in walls["ask_walls"])
    return True

def test_order_book_analyzer() -> bool:
    analyzer = OrderBookAnalyzer(max_snapshots=50)
    assert analyzer.is_empty and analyzer.current is None
    base_time = _now_ms()
    for i in range(20):
        snap = OrderBookSnapshot.from_raw(symbol="TEST/USDT", bids=[(100, 10 + i * 2)], asks=[(101, 10)], timestamp=base_time + i * 100, sequence=i + 1)
        analyzer.add_snapshot(snap)
    assert analyzer.count == 20 and not analyzer.is_empty and analyzer.current is not None and analyzer.is_data_fresh(10000)
    assert analyzer.imbalance_trend() > 0 and analyzer.is_bullish() and not analyzer.is_bearish() and analyzer.get_signal_strength() > 0
    stats = analyzer.get_statistics()
    assert "mid_price" in stats and "signal_strength" in stats
    return True

def test_sequence_gap_detection() -> bool:
    analyzer = OrderBookAnalyzer()
    for seq in [1, 2, 5, 6, 10]:
        snap = OrderBookSnapshot.from_raw(symbol="TEST/USDT", bids=[(100, 10)], asks=[(101, 10)], sequence=seq)
        analyzer.add_snapshot(snap)
    return analyzer.sequence_gaps == 5

def test_tick_classifier() -> bool:
    classifier = TickClassifier()
    prices = [100.0, 100.1, 100.2, 100.2, 100.1, 100.0, 100.05]
    expected = [TradeSide.UNKNOWN, TradeSide.BUY, TradeSide.BUY, TradeSide.BUY, TradeSide.SELL, TradeSide.SELL, TradeSide.BUY]
    result = classifier.classify_batch(prices)
    assert result == expected
    classifier.reset()
    assert classifier.classify(100.0) == TradeSide.UNKNOWN
    return True

def test_synthetic_order_book() -> bool:
    ticks = [(i * 1000, 100.0 + (i % 10) * 0.01) for i in range(100)]
    snap = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)
    assert snap is not None and snap.is_valid and snap.symbol == "SYNTHETIC"
    snap2 = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)
    assert snap.best_bid == snap2.best_bid and snap.best_ask == snap2.best_ask
    snap3 = SyntheticOrderBook.from_candle(timestamp=_now_ms(), open_price=100, high_price=105, low_price=95, close_price=102, volume=1000, random_seed=42)
    assert snap3.is_valid and abs(snap3.mid_price - 102) < 1
    analyzer = SyntheticOrderBook.generate_analyzer(ticks, sample_every=10)
    assert analyzer is not None and analyzer.count == 10
    return True

def test_data_validator() -> bool:
    validator = DataValidator(max_spread_bps=100, max_price_spike_pct=5.0, max_symbols=10)
    valid, _ = validator.validate(OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [(101, 10)]))
    assert valid
    crossed, _ = validator.validate(OrderBookSnapshot.from_raw("TEST/USDT", [(102, 10)], [(101, 10)]))
    assert not crossed
    wide_valid, wide_warnings = validator.validate(OrderBookSnapshot.from_raw("TEST/USDT", [(90, 10)], [(110, 10)]))
    assert any("spread" in w.lower() for w in wide_warnings)
    for i in range(20):
        validator.validate(OrderBookSnapshot.from_raw("SPIKE/USDT", [(100, 10)], [(101, 10)]))
    spike_valid, _ = validator.validate(OrderBookSnapshot.from_raw("SPIKE/USDT", [(150, 10)], [(151, 10)]))
    assert not spike_valid
    for i in range(20):
        validator.validate(OrderBookSnapshot.from_raw(f"SYM{i}/USDT", [(100, 10)], [(101, 10)]))
    stats = validator.get_stats()
    assert stats["tracked_symbols"] <= stats["max_symbols"]
    return True

def test_delta_handler() -> bool:
    handler = OrderBookDeltaHandler("TEST/USDT", max_levels=10)
    handler.apply_snapshot(bids=[(100, 10), (99, 20)], asks=[(101, 15), (102, 25)], update_id=1)
    snap = handler.get_snapshot()
    assert snap is not None and snap.best_bid == 100 and snap.best_ask == 101
    snap2 = handler.apply_delta(bids=[(100, 15)], asks=[(101, 20)], update_id=2)
    assert snap2.bids[0].size == 15 and snap2.asks[0].size == 20
    snap3 = handler.apply_delta(bids=[(100, 0)], asks=[], update_id=3)
    assert snap3.best_bid == 99
    snap4 = handler.apply_delta(bids=[(98, 100)], asks=[], update_id=2)
    assert snap4 is None
    return True

def test_alert_manager() -> bool:
    alerts = AlertManager(throttle_seconds=1)
    received = []
    alerts.add_handler(lambda level, msg, ctx: received.append((level, msg, ctx)))
    assert alerts.info("Test info") and alerts.warning("Test warning") and alerts.critical("Test critical")
    assert len(received) == 3 and received[0][0] == "INFO" and received[1][0] == "WARNING" and received[2][0] == "CRITICAL"
    received.clear()
    assert alerts.warning("First", throttle_key="test_key")
    assert not alerts.warning("Second", throttle_key="test_key")
    assert len(received) == 1
    assert alerts.warning("Third", throttle_key="other_key")
    assert len(received) == 2
    alerts.clear_throttle()
    assert alerts.warning("After clear", throttle_key="test_key")
    return True

def test_state_manager() -> bool:
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test_state.json")
        state1 = StateManager(state_file)
        state1.set("key1", "value1")
        state1.set("key2", 123)
        state1.update({"key3": [1, 2, 3]})
        state1.update_positions({"BTC/USDT": 0.5, "ETH/USDT": 2.0})
        state1.set_running(True)
        state2 = StateManager(state_file)
        assert state2.get("key1") == "value1" and state2.get("key2") == 123 and state2.get("key3") == [1, 2, 3]
        assert state2.get_positions()["BTC/USDT"] == 0.5 and state2.was_running() == True
        state2.delete("key1")
        assert state2.get("key1") is None
        state2.clear()
        assert state2.get_all() == {}
        Path(state_file).write_text("not valid json {{{")
        state3 = StateManager(state_file)
        assert state3.get("key1") is None
    return True

def test_throttled_callback() -> bool:
    calls = []
    tc = ThrottledCallback(callback=lambda x: calls.append(x), min_interval_ms=100)
    for i in range(10):
        tc(i)
    assert len(calls) == 1 and calls[0] == 0
    time.sleep(0.15)
    tc(99)
    assert len(calls) == 2 and calls[1] == 99
    tc(100)
    tc.flush()
    assert len(calls) == 3 and calls[2] == 100
    return True

def test_backtest_provider() -> bool:
    provider = BacktestOrderBookProvider()
    candles = [{"timestamp": i * 60000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for i in range(50)]
    provider.load_from_candles("BTC/USDT", candles)
    ticks = [(i * 1000, 200.0 + i * 0.1) for i in range(100)]
    provider.load_from_ticks("ETH/USDT", ticks, sample_every=10)
    assert "BTC/USDT" in provider.get_all_symbols() and "ETH/USDT" in provider.get_all_symbols()
    btc_analyzer = provider.get_analyzer("BTC/USDT")
    assert btc_analyzer is not None and btc_analyzer.count == 50
    snap_at_25 = provider.get_snapshot_at_time("BTC/USDT", 25 * 60000)
    assert snap_at_25 is not None and snap_at_25.timestamp <= 25 * 60000
    provider.clear("BTC/USDT")
    assert provider.get_analyzer("BTC/USDT") is None
    provider.clear()
    assert len(provider.get_all_symbols()) == 0
    return True

def test_stream_health() -> bool:
    health = StreamHealth()
    assert not health.is_healthy and health.age_ms > 100000
    health.connected, health.last_update_ms, health.latency_ms = True, _now_ms(), 50
    assert health.is_healthy and health.age_ms < 1000
    for i in range(15):
        health.record_error(f"Error {i}")
    assert not health.is_healthy and health.error_count == 15
    health.reset_errors()
    assert health.is_healthy and health.error_count == 0
    health.record_reconnect()
    health.record_reconnect()
    assert health.reconnect_count == 2
    data = health.to_dict()
    assert "connected" in data and "latency_ms" in data and "is_healthy" in data
    return True

def test_crossed_book_handling() -> bool:
    normal = OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [(101, 10)])
    assert not normal.is_crossed
    crossed = OrderBookSnapshot.from_raw("TEST/USDT", [(102, 10)], [(101, 10)])
    assert crossed.is_crossed
    equal = OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [(100, 10)])
    assert equal.is_crossed
    return True

def test_empty_book_handling() -> bool:
    empty_bids = OrderBookSnapshot.from_raw("TEST/USDT", [], [(101, 10)])
    assert not empty_bids.is_valid and empty_bids.mid_price == 0.0 and empty_bids.spread == 0.0
    empty_asks = OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [])
    assert not empty_asks.is_valid
    empty = OrderBookSnapshot.from_raw("TEST/USDT", [], [])
    assert not empty.is_valid
    return True

def test_stale_data_detection() -> bool:
    fresh = OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [(101, 10)], timestamp=_now_ms())
    assert not fresh.is_stale(5000)
    stale = OrderBookSnapshot.from_raw("TEST/USDT", [(100, 10)], [(101, 10)], timestamp=_now_ms() - 10000)
    assert stale.is_stale(5000) and not stale.is_stale(15000)
    return True

def test_thread_safety() -> bool:
    analyzer = OrderBookAnalyzer()
    errors = []
    def writer():
        for i in range(100):
            try:
                snap = OrderBookSnapshot.from_raw("TEST/USDT", [(100 + i * 0.01, 10)], [(101 + i * 0.01, 10)], sequence=i)
                analyzer.add_snapshot(snap)
                time.sleep(0.001)
            except Exception as e:
                errors.append(f"Writer: {e}")
    def reader():
        for _ in range(100):
            try:
                _ = analyzer.current
                _ = analyzer.count
                _ = analyzer.get_statistics()
                _ = analyzer.get_recent(10)
                time.sleep(0.001)
            except Exception as e:
                errors.append(f"Reader: {e}")
    threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        print(f"   Errors: {errors[:5]}")
        return False
    return True

def test_symbol_validation() -> bool:
    valid = ["BTC/USDT", "ETH/BTC", "1INCH/USDT", "XRP/USDT"]
    for s in valid:
        if not OrderBookConstants.SYMBOL_PATTERN.match(s):
            print(f"   Should be valid: {s}")
            return False
    invalid = ["btc/usdt", "BTC-USDT", "BTC", "BTC/", "/USDT", "BTC USDT"]
    for s in invalid:
        if OrderBookConstants.SYMBOL_PATTERN.match(s):
            print(f"   Should be invalid: {s}")
            return False
    return True

# ============================================================
# LIVE TESTS
# ============================================================

def test_live_single_symbol(duration: int = 15) -> bool:
    if not HAS_WEBSOCKETS:
        print("   Skipped: websockets not installed")
        return True
    manager = OrderBookManager(store_history=False)
    updates = []
    def callback(snap):
        updates.append({"time": time.time(), "mid": snap.mid_price, "spread_bps": snap.spread_bps, "imbalance": snap.imbalance})
    manager.subscribe("BTC/USDT", callback)
    start = time.time()
    while time.time() - start < duration:
        time.sleep(1)
        if len(updates) > 0 and len(updates) % 5 == 0:
            print(f"      Updates: {len(updates)}, Rate: {len(updates)/(time.time()-start):.1f}/s")
    manager.stop()
    print(f"   Total updates: {len(updates)}")
    return len(updates) > duration

def test_live_multi_symbol(duration: int = 15, num_symbols: int = 5) -> bool:
    if not HAS_WEBSOCKETS:
        print("   Skipped: websockets not installed")
        return True
    symbols = ALL_TRADING_SYMBOLS[:num_symbols]
    manager = OrderBookManager(multi_symbol=True, store_history=False)
    counts = {s: 0 for s in symbols}
    manager.subscribe_many(symbols, lambda snap: counts.__setitem__(snap.symbol, counts.get(snap.symbol, 0) + 1))
    start = time.time()
    while time.time() - start < duration:
        time.sleep(2)
        active, total = sum(1 for c in counts.values() if c > 0), sum(counts.values())
        print(f"      Active: {active}/{len(symbols)}, Total: {total}")
    manager.stop()
    active_count = sum(1 for c in counts.values() if c > 0)
    return active_count >= len(symbols) * 0.8

def test_live_health_monitoring(duration: int = 10) -> bool:
    if not HAS_WEBSOCKETS:
        print("   Skipped: websockets not installed")
        return True
    manager = OrderBookManager(multi_symbol=True, enable_validation=True, enable_alerts=True)
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    manager.subscribe_many(symbols)
    time.sleep(duration)
    ready, issues = manager.is_ready_for_trading(symbols)
    report = manager.get_health_report()
    print(f"   Ready: {ready}, Healthy: {report['healthy_count']}/{report['total_symbols']}")
    if issues:
        print(f"   Issues: {issues}")
    manager.stop()
    return report['healthy_count'] > 0

def test_manager_lifecycle(duration: int = 5) -> bool:
    if not HAS_WEBSOCKETS:
        print("   Skipped: websockets not installed")
        return True
    manager = OrderBookManager(store_history=False, enable_validation=True, enable_alerts=True)
    manager.subscribe("BTC/USDT")
    time.sleep(duration)
    snap = manager.get_snapshot("BTC/USDT")
    if snap is None or not snap.is_valid:
        print("   First subscribe failed")
        manager.stop()
        return False
    manager.unsubscribe("BTC/USDT")
    time.sleep(1)
    manager.subscribe("ETH/USDT")
    time.sleep(duration)
    snap2 = manager.get_snapshot("ETH/USDT")
    manager.stop()
    if snap2 is None or not snap2.is_valid:
        print(f"   Second subscribe failed: snap2={snap2}")
        return False
    return True

# ============================================================
# INTEGRATION TESTS
# ============================================================

def test_backtest_workflow() -> bool:
    manager = OrderBookManager(backtest_mode=True)
    random.seed(42)
    candles = [{"timestamp": i * 60000, "open": 50000 + random.uniform(-500, 500), "high": 50000 + random.uniform(0, 600), "low": 50000 - random.uniform(0, 600), "close": 50000 + random.uniform(-300, 300), "volume": random.uniform(100, 1000)} for i in range(100)]
    manager.load_backtest_candles("BTC/USDT", candles)
    signals = []
    for i in range(10, 100, 5):
        ts = i * 60000
        manager.set_backtest_time(ts)
        snap = manager.get_backtest_snapshot("BTC/USDT", ts)
        analyzer = manager.get_analyzer("BTC/USDT")
        if snap and analyzer:
            signals.append({"time": ts, "mid_price": snap.mid_price, "signal": analyzer.get_signal_strength(), "bullish": analyzer.is_bullish(), "bearish": analyzer.is_bearish()})
    manager.stop()
    assert len(signals) > 0
    print(f"   Generated {len(signals)} trading signals")
    return True

# ============================================================
# PERFORMANCE TESTS
# ============================================================

def test_snapshot_creation_performance() -> bool:
    iterations = 10000
    start = time.time()
    for i in range(iterations):
        OrderBookSnapshot.from_raw("TEST/USDT", [(100 + j * 0.01, 10) for j in range(20)], [(101 + j * 0.01, 10) for j in range(20)])
    raw_time = time.time() - start
    bids = [OrderBookLevel(100 - j * 0.01, 10) for j in range(20)]
    asks = [OrderBookLevel(101 + j * 0.01, 10) for j in range(20)]
    start = time.time()
    for i in range(iterations):
        OrderBookSnapshot.from_sorted("TEST/USDT", bids.copy(), asks.copy())
    sorted_time = time.time() - start
    print(f"   from_raw: {iterations/raw_time:.0f}/sec\n   from_sorted: {iterations/sorted_time:.0f}/sec\n   Speedup: {raw_time/sorted_time:.1f}x")
    return raw_time > 0 and sorted_time > 0

def test_analyzer_performance() -> bool:
    analyzer = OrderBookAnalyzer(max_snapshots=1000)
    for i in range(1000):
        snap = OrderBookSnapshot.from_raw("TEST/USDT", [(100 + random.uniform(-1, 1), random.uniform(1, 100))], [(101 + random.uniform(-1, 1), random.uniform(1, 100))], sequence=i)
        analyzer.add_snapshot(snap)
    iterations = 1000
    start = time.time()
    for _ in range(iterations):
        _ = analyzer.get_signal_strength()
    signal_time = time.time() - start
    start = time.time()
    for _ in range(iterations):
        _ = analyzer.get_statistics()
    stats_time = time.time() - start
    print(f"   get_signal_strength: {iterations/signal_time:.0f}/sec\n   get_statistics: {iterations/stats_time:.0f}/sec")
    return signal_time > 0 and stats_time > 0

# ============================================================
# MAIN TEST RUNNER
# ============================================================

def run_all_tests(include_live: bool = True, include_performance: bool = True) -> bool:
    print("\n" + "=" * 70 + "\n ORACLE ORDER BOOK v3.3 - COMPREHENSIVE TEST SUITE\n" + "=" * 70)
    print(f" WebSockets: {HAS_WEBSOCKETS}\n Time: {datetime.now().isoformat()}\n" + "=" * 70)
    
    print("\n" + "-" * 70 + "\n UNIT TESTS\n" + "-" * 70)
    run_test("OrderBookLevel", test_order_book_level)
    run_test("OrderBookSnapshot Creation", test_order_book_snapshot_creation)
    run_test("OrderBookSnapshot Metrics", test_order_book_metrics)
    run_test("Market Impact", test_market_impact)
    run_test("Wall Detection", test_wall_detection)
    run_test("OrderBookAnalyzer", test_order_book_analyzer)
    run_test("Sequence Gap Detection", test_sequence_gap_detection)
    run_test("Tick Classifier", test_tick_classifier)
    run_test("Synthetic Order Book", test_synthetic_order_book)
    run_test("Data Validator", test_data_validator)
    run_test("Delta Handler", test_delta_handler)
    run_test("Alert Manager", test_alert_manager)
    run_test("State Manager", test_state_manager)
    run_test("Throttled Callback", test_throttled_callback)
    run_test("Backtest Provider", test_backtest_provider)
    run_test("Stream Health", test_stream_health)
    run_test("Crossed Book Handling", test_crossed_book_handling)
    run_test("Empty Book Handling", test_empty_book_handling)
    run_test("Stale Data Detection", test_stale_data_detection)
    run_test("Thread Safety", test_thread_safety)
    run_test("Symbol Validation", test_symbol_validation)
    
    if include_live and HAS_WEBSOCKETS:
        print("\n" + "-" * 70 + "\n LIVE TESTS\n" + "-" * 70)
        run_test("Live Single Symbol", test_live_single_symbol, 10)
        run_test("Live Multi Symbol", test_live_multi_symbol, 10, 5)
        run_test("Live Health Monitoring", test_live_health_monitoring, 8)
        run_test("Manager Lifecycle", test_manager_lifecycle, 5)
    
    print("\n" + "-" * 70 + "\n INTEGRATION TESTS\n" + "-" * 70)
    run_test("Backtest Workflow", test_backtest_workflow)
    
    if include_performance:
        print("\n" + "-" * 70 + "\n PERFORMANCE TESTS\n" + "-" * 70)
        run_test("Snapshot Creation Performance", test_snapshot_creation_performance)
        run_test("Analyzer Performance", test_analyzer_performance)
    
    print(results.summary())
    if results.errors:
        print(" ERRORS:")
        for err in results.errors[:10]:
            print(f"   - {err}")
    
    return results.failed == 0

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test oracle_orderbook.py")
    parser.add_argument("--no-live", action="store_true", help="Skip live tests")
    parser.add_argument("--no-perf", action="store_true", help="Skip performance tests")
    parser.add_argument("--quick", action="store_true", help="Quick test (no live/perf)")
    args = parser.parse_args()
    success = run_all_tests(include_live=not (args.no_live or args.quick), include_performance=not (args.no_perf or args.quick))
    sys.exit(0 if success else 1)
