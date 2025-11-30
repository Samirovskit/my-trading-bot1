#!/usr/bin/env python3
"""
oracle_orderbook_test.py - ORACLE ORDER BOOK TEST SUITE v5.0

The Benchmark Suite From Hell.
Tests the oracle_orderbook module in every possible way and shape.

Usage:
    python3 oracle_orderbook_test.py                    # Show help
    python3 oracle_orderbook_test.py unit               # Fast unit tests (no network)
    python3 oracle_orderbook_test.py bench              # Performance benchmarks
    python3 oracle_orderbook_test.py bench parsing      # Benchmark parsing only
    python3 oracle_orderbook_test.py live [sec]         # Live connectivity test
    python3 oracle_orderbook_test.py multi [sec] [n]    # Multi-symbol test
    python3 oracle_orderbook_test.py stress [sec]       # Full stress test
    python3 oracle_orderbook_test.py reconnect [sec]    # Reconnection stability test
    python3 oracle_orderbook_test.py diagnose [sec]     # Diagnose unhealthy symbols
    python3 oracle_orderbook_test.py memory             # Memory profiling
    python3 oracle_orderbook_test.py backtest           # Backtest simulation test
    python3 oracle_orderbook_test.py execution          # Execution simulator test
    python3 oracle_orderbook_test.py integration        # Full integration tests
    python3 oracle_orderbook_test.py all                # Run everything
    python3 oracle_orderbook_test.py quick              # Quick smoke test

Examples:
    python3 oracle_orderbook_test.py stress 86400       # 24-hour stress test
    python3 oracle_orderbook_test.py diagnose 300       # 5-minute diagnosis
    python3 oracle_orderbook_test.py bench pipeline     # Benchmark pipeline only

Author: Oracle Trading System
Version: 5.0
"""

from __future__ import annotations

import argparse
import gc
import os
import random
import statistics
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ============================================================
# IMPORTS FROM ORACLE ORDERBOOK
# ============================================================

try:
    from oracle_orderbook import (
        OrderBookSnapshot,
        OrderBookLevel,
        OrderBookAnalyzer,
        OrderBookManager,
        BinanceMultiStream,
        BinanceOrderBookStream,
        BacktestOrderBookProvider,
        SyntheticOrderBook,
        BacktestExecutionSimulator,
        SlippageEstimator,
        OrderFlowTracker,
        OrderFlowMetrics,
        TickClassifier,
        TradeSide,
        DataValidator,
        AlertManager,
        StateManager,
        ThrottledCallback,
        OrderBookDeltaHandler,
        StreamHealth,
        OrderBookConstants,
        ALL_TRADING_SYMBOLS,
        HAS_WEBSOCKETS,
        HAS_CCXT_PRO,
        _now_ms,
    )
    IMPORT_SUCCESS = True
except ImportError as e:
    print(f"❌ Failed to import oracle_orderbook: {e}")
    print("   Make sure oracle_orderbook.py is in the same directory or PYTHONPATH")
    IMPORT_SUCCESS = False
    sys.exit(1)


# ============================================================
# TEST FRAMEWORK
# ============================================================

@dataclass
class TestResult:
    """Result of a single test."""
    name: str
    passed: bool
    duration: float
    error: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    """Result of a benchmark run."""
    name: str
    mean_ms: float
    median_ms: float
    min_ms: float
    max_ms: float
    std_ms: float
    ops_per_sec: float
    iterations: int

    def speedup_vs(self, other: 'BenchmarkResult') -> float:
        """Calculate speedup compared to another benchmark."""
        if self.mean_ms == 0:
            return float('inf')
        return other.mean_ms / self.mean_ms


class TestSuite:
    """
    Unified test suite framework.
    
    Features:
    - Automatic test discovery and execution
    - Timing and statistics
    - Rich output formatting
    - Error collection and reporting
    """

    def __init__(self, name: str = "Test Suite"):
        """Initialize test suite."""
        self.name = name
        self.results: List[TestResult] = []
        self.benchmarks: Dict[str, BenchmarkResult] = {}
        self.start_time = time.time()

    def run_test(
        self,
        name: str,
        test_fn: Callable[..., bool],
        *args,
        **kwargs
    ) -> bool:
        """Run a single test with error handling and timing."""
        print(f"\n[TEST] {name}...", end="", flush=True)
        start = time.perf_counter()

        try:
            success = test_fn(*args, **kwargs)
            duration = time.perf_counter() - start

            status = "✅" if success else "❌"
            print(f" {status} ({duration:.3f}s)")

            self.results.append(TestResult(
                name=name,
                passed=success,
                duration=duration
            ))
            return success

        except Exception as e:
            duration = time.perf_counter() - start
            print(f" ❌ ERROR ({duration:.3f}s)")
            print(f"    {type(e).__name__}: {e}")

            if os.environ.get("DEBUG"):
                traceback.print_exc()

            self.results.append(TestResult(
                name=name,
                passed=False,
                duration=duration,
                error=str(e)
            ))
            return False

    def benchmark(
        self,
        name: str,
        func: Callable[[], Any],
        iterations: int = 10000,
        warmup: int = 100
    ) -> BenchmarkResult:
        """Run a benchmark with statistical analysis."""
        for _ in range(warmup):
            func()

        gc.collect()
        gc.disable()

        times = []
        try:
            for _ in range(iterations):
                start = time.perf_counter_ns()
                func()
                end = time.perf_counter_ns()
                times.append((end - start) / 1_000_000)
        finally:
            gc.enable()

        mean_time = statistics.mean(times)
        result = BenchmarkResult(
            name=name,
            mean_ms=mean_time,
            median_ms=statistics.median(times),
            min_ms=min(times),
            max_ms=max(times),
            std_ms=statistics.stdev(times) if len(times) > 1 else 0,
            ops_per_sec=1000 / mean_time if mean_time > 0 else float('inf'),
            iterations=iterations
        )

        self.benchmarks[name] = result
        return result

    def print_benchmark(self, result: BenchmarkResult) -> None:
        """Pretty print a benchmark result."""
        print(f"\n  📊 {result.name}:")
        print(f"     Mean:       {result.mean_ms:.4f} ms")
        print(f"     Median:     {result.median_ms:.4f} ms")
        print(f"     Min/Max:    {result.min_ms:.4f} / {result.max_ms:.4f} ms")
        print(f"     Std Dev:    {result.std_ms:.4f} ms")
        print(f"     Throughput: {result.ops_per_sec:,.0f} ops/sec")

    def compare_benchmarks(
        self,
        name1: str,
        result1: BenchmarkResult,
        name2: str,
        result2: BenchmarkResult
    ) -> None:
        """Compare two benchmarks."""
        if result2.mean_ms > 0:
            speedup = result1.mean_ms / result2.mean_ms
        else:
            speedup = float('inf')

        direction = "faster" if speedup > 1 else "slower"

        print(f"\n  ⚡ Comparison: {name1} vs {name2}")
        print(f"     {name1}: {result1.mean_ms:.4f} ms ({result1.ops_per_sec:,.0f} ops/sec)")
        print(f"     {name2}: {result2.mean_ms:.4f} ms ({result2.ops_per_sec:,.0f} ops/sec)")
        print(f"     Result: {name2} is {speedup:.2f}x {direction}")

    def summary(self) -> Dict[str, Any]:
        """Generate test summary."""
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        total_time = time.time() - self.start_time

        return {
            "total": len(self.results),
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / max(1, len(self.results)) * 100,
            "duration": total_time,
            "errors": [r for r in self.results if r.error],
        }

    def print_summary(self) -> None:
        """Print formatted summary."""
        s = self.summary()

        print("\n" + "=" * 70)
        print(f" {self.name} RESULTS")
        print("=" * 70)
        print(f" Passed:   {s['passed']}/{s['total']} ({s['pass_rate']:.1f}%)")
        print(f" Failed:   {s['failed']}")
        print(f" Duration: {s['duration']:.2f}s")

        if s['errors']:
            print("\n ERRORS:")
            for r in s['errors'][:10]:
                print(f"   • {r.name}: {r.error[:100]}")

        print("=" * 70)

        if s['failed'] == 0:
            print(" ✅ ALL TESTS PASSED")
        else:
            print(f" ❌ {s['failed']} TESTS FAILED")


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def generate_binance_data(levels: int = 20) -> Tuple[List[List[str]], List[List[str]]]:
    """Generate realistic Binance-style order book data."""
    base_price = 50000.0 + random.uniform(-1000, 1000)
    spread = base_price * 0.0001

    bids = []
    asks = []

    for i in range(levels):
        bid_price = base_price - spread / 2 - i * spread * 0.5
        ask_price = base_price + spread / 2 + i * spread * 0.5
        size = random.uniform(0.1, 10.0)

        bids.append([f"{bid_price:.2f}", f"{size:.8f}"])
        asks.append([f"{ask_price:.2f}", f"{size:.8f}"])

    return bids, asks


def generate_raw_data(levels: int = 20) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Generate raw price/size tuple data."""
    base_price = 50000.0 + random.uniform(-1000, 1000)
    spread = base_price * 0.0001

    bids = []
    asks = []

    for i in range(levels):
        bid_price = base_price - spread / 2 - i * spread * 0.5
        ask_price = base_price + spread / 2 + i * spread * 0.5
        size = random.uniform(0.1, 10.0)

        bids.append((bid_price, size))
        asks.append((ask_price, size))

    return bids, asks


def timed_loop(
    duration: int,
    interval: float,
    callback: Callable[[float], None],
    stop_event: Optional[threading.Event] = None
) -> None:
    """Run a timed loop with periodic callbacks."""
    start = time.time()
    try:
        while time.time() - start < duration:
            if stop_event and stop_event.is_set():
                break
            callback(time.time() - start)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n   Interrupted by user")


def print_header(title: str) -> None:
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def print_subheader(title: str) -> None:
    """Print a subsection header."""
    print("\n" + "-" * 70)
    print(f" {title}")
    print("-" * 70)


def print_stats(title: str, stats: Dict[str, Any]) -> None:
    """Print statistics in a formatted way."""
    print(f"\n  {title}:")
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"     {key}: {value:.4f}")
        else:
            print(f"     {key}: {value}")


# ============================================================
# UNIT TESTS
# ============================================================

class UnitTests:
    """All unit tests - no network required."""

    # ─────────────────────────────────────────────────────────
    # ORDER BOOK LEVEL TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_order_book_level() -> bool:
        """Test OrderBookLevel creation and validation."""
        level = OrderBookLevel(100.0, 10.0)
        if level.price != 100.0:
            return False
        if level.size != 10.0:
            return False
        if level.value != 1000.0:
            return False
        if level.to_tuple() != (100.0, 10.0):
            return False

        level2 = OrderBookLevel("99.5", "5.5")
        if level2.price != 99.5:
            return False
        if level2.size != 5.5:
            return False

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

        try:
            OrderBookLevel("invalid", 10)
            return False
        except ValueError:
            pass

        return True

    # ─────────────────────────────────────────────────────────
    # ORDER BOOK SNAPSHOT CREATION TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_snapshot_from_raw() -> bool:
        """Test OrderBookSnapshot.from_raw creation."""
        snap = OrderBookSnapshot.from_raw(
            symbol="BTC/USDT",
            bids=[(100, 10), (99, 20), (98, 30)],
            asks=[(101, 15), (102, 25), (103, 35)],
            timestamp=_now_ms(),
            sequence=12345,
            exchange="test"
        )

        if not snap.is_valid:
            return False
        if snap.is_crossed:
            return False
        if snap.best_bid != 100:
            return False
        if snap.best_ask != 101:
            return False
        if snap.best_bid_size != 10:
            return False
        if snap.best_ask_size != 15:
            return False
        if snap.symbol != "BTC/USDT":
            return False
        if snap.sequence != 12345:
            return False

        return True

    @staticmethod
    def test_snapshot_from_sorted() -> bool:
        """Test OrderBookSnapshot.from_sorted creation."""
        bids = [OrderBookLevel(100, 10), OrderBookLevel(99, 20)]
        asks = [OrderBookLevel(101, 15), OrderBookLevel(102, 25)]

        snap = OrderBookSnapshot.from_sorted(
            symbol="ETH/USDT",
            bids=bids,
            asks=asks,
            timestamp=_now_ms()
        )

        if not snap.is_valid:
            return False
        if snap.best_bid != 100:
            return False
        if snap.best_ask != 101:
            return False

        return True

    @staticmethod
    def test_snapshot_from_binance_fast() -> bool:
        """Test OrderBookSnapshot.from_binance_fast ultra-fast path."""
        bids = [["50000.00", "1.5"], ["49999.00", "2.0"], ["49998.00", "2.5"]]
        asks = [["50001.00", "1.0"], ["50002.00", "1.5"], ["50003.00", "2.0"]]

        snap = OrderBookSnapshot.from_binance_fast(
            symbol="BTC/USDT",
            bids=bids,
            asks=asks,
            timestamp=_now_ms(),
            sequence=999999
        )

        if not snap.is_valid:
            return False
        if snap.exchange != "binance":
            return False
        if snap.best_bid != 50000.00:
            return False
        if snap.best_ask != 50001.00:
            return False
        if len(snap.bids) != 3:
            return False
        if len(snap.asks) != 3:
            return False

        return True

    @staticmethod
    def test_snapshot_serialization() -> bool:
        """Test snapshot serialization to/from dict."""
        snap1 = OrderBookSnapshot.from_raw(
            symbol="BTC/USDT",
            bids=[(100, 10), (99, 20)],
            asks=[(101, 15), (102, 25)],
            timestamp=1234567890,
            sequence=999
        )

        data = snap1.to_dict()

        if data["symbol"] != "BTC/USDT":
            return False
        if data["timestamp"] != 1234567890:
            return False
        if data["sequence"] != 999:
            return False

        snap2 = OrderBookSnapshot.from_dict(data)

        if snap2.best_bid != snap1.best_bid:
            return False
        if snap2.best_ask != snap1.best_ask:
            return False
        if snap2.symbol != snap1.symbol:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # ORDER BOOK SNAPSHOT METRICS TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_snapshot_basic_metrics() -> bool:
        """Test basic OrderBookSnapshot metrics."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 50), (99, 30)],
            [(101, 25), (102, 35)]
        )

        if snap.mid_price != 100.5:
            return False

        if snap.spread != 1.0:
            return False

        expected_spread_pct = (1.0 / 100.5) * 100
        if abs(snap.spread_pct - expected_spread_pct) > 0.001:
            return False

        expected_spread_bps = expected_spread_pct * 100
        if abs(snap.spread_bps - expected_spread_bps) > 0.1:
            return False

        expected_imbalance = (50 - 25) / (50 + 25)
        if abs(snap.imbalance - expected_imbalance) > 0.001:
            return False

        return True

    @staticmethod
    def test_snapshot_volume_metrics() -> bool:
        """Test volume and value metrics."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20), (98, 30)],
            [(101, 15), (102, 25), (103, 35)]
        )

        if snap.total_bid_volume(3) != 60:
            return False
        if snap.total_ask_volume(3) != 75:
            return False

        expected_bid_value = 100 * 10 + 99 * 20 + 98 * 30
        if abs(snap.total_bid_value(3) - expected_bid_value) > 0.01:
            return False

        return True

    @staticmethod
    def test_snapshot_depth_imbalance() -> bool:
        """Test depth imbalance calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 100), (99, 100)],
            [(101, 50), (102, 50)]
        )

        depth_imb = snap.depth_imbalance(2)
        expected = (200 - 100) / (200 + 100)
        if abs(depth_imb - expected) > 0.001:
            return False

        return True

    @staticmethod
    def test_snapshot_value_imbalance() -> bool:
        """Test value imbalance calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 15), (102, 25)]
        )

        bid_value = 100 * 10 + 99 * 20
        ask_value = 101 * 15 + 102 * 25

        val_imb = snap.value_imbalance(2)
        expected = (bid_value - ask_value) / (bid_value + ask_value)

        if abs(val_imb - expected) > 0.001:
            return False

        return True

    @staticmethod
    def test_snapshot_vwap() -> bool:
        """Test VWAP calculations."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 15), (102, 25)]
        )

        vwap_bid = snap.vwap_bid(2)
        expected_vwap_bid = (100 * 10 + 99 * 20) / (10 + 20)
        if abs(vwap_bid - expected_vwap_bid) > 0.001:
            return False

        vwap_ask = snap.vwap_ask(2)
        expected_vwap_ask = (101 * 15 + 102 * 25) / (15 + 25)
        if abs(vwap_ask - expected_vwap_ask) > 0.001:
            return False

        return True

    @staticmethod
    def test_snapshot_microprice() -> bool:
        """Test microprice calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 30)],
            [(102, 70)]
        )

        microprice = snap.microprice()
        expected = (100 * 70 + 102 * 30) / (30 + 70)
        if abs(microprice - expected) > 0.001:
            return False

        return True

    @staticmethod
    def test_snapshot_weighted_mid() -> bool:
        """Test weighted mid price calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 15), (102, 25)]
        )

        weighted_mid = snap.weighted_mid_price(2)
        if weighted_mid <= 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # MARKET IMPACT TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_market_impact_small() -> bool:
        """Test market impact for small orders."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 10), (98, 10)],
            [(101, 10), (102, 10), (103, 10)]
        )

        impact_buy = snap.market_impact(5, "buy")
        if impact_buy != 101.0:
            return False

        impact_sell = snap.market_impact(5, "sell")
        if impact_sell != 100.0:
            return False

        return True

    @staticmethod
    def test_market_impact_large() -> bool:
        """Test market impact for large orders."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 10), (98, 10)],
            [(101, 10), (102, 10), (103, 10)]
        )

        impact = snap.market_impact(15, "buy")
        expected = (10 * 101 + 5 * 102) / 15
        if abs(impact - expected) > 0.01:
            return False

        impact_sell = snap.market_impact(25, "sell")
        expected_sell = (10 * 100 + 10 * 99 + 5 * 98) / 25
        if abs(impact_sell - expected_sell) > 0.01:
            return False

        return True

    @staticmethod
    def test_market_impact_zero() -> bool:
        """Test market impact edge cases."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10)],
            [(101, 10)]
        )

        impact_zero = snap.market_impact(0, "buy")
        if impact_zero != snap.mid_price:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # WALL DETECTION TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_wall_detection() -> bool:
        """Test order wall detection."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 150), (98, 10), (97, 10)],
            [(101, 10), (102, 200), (103, 10), (104, 10)]
        )

        walls = snap.find_walls(threshold_mult=2.5)

        if len(walls["bid_walls"]) < 1:
            return False

        bid_wall_prices = [w.price for w in walls["bid_walls"]]
        if 99 not in bid_wall_prices:
            return False

        if len(walls["ask_walls"]) < 1:
            return False

        ask_wall_prices = [w.price for w in walls["ask_walls"]]
        if 102 not in ask_wall_prices:
            return False

        return True

    @staticmethod
    def test_nearest_walls() -> bool:
        """Test nearest wall detection."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 100), (98, 10)],
            [(101, 10), (102, 100), (103, 10)]
        )

        bid_wall = snap.nearest_bid_wall(threshold_mult=2.0)
        if bid_wall is None:
            return False

        ask_wall = snap.nearest_ask_wall(threshold_mult=2.0)
        if ask_wall is None:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # LIQUIDITY TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_liquidity_at_price() -> bool:
        """Test liquidity at price lookup."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99.5, 20), (99, 30)],
            [(101, 15), (101.5, 25), (102, 35)]
        )

        bid_liq, ask_liq = snap.liquidity_at_price(100, tolerance_pct=2.0)
        if bid_liq <= 0:
            return False

        return True

    @staticmethod
    def test_depth_at_distance() -> bool:
        """Test depth at distance calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20), (98, 30)],
            [(101, 15), (102, 25), (103, 35)]
        )

        bid_depth, ask_depth = snap.depth_at_distance(pct_from_mid=2.0)
        if bid_depth <= 0:
            return False
        if ask_depth <= 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # SNAPSHOT VALIDATION TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_snapshot_is_valid() -> bool:
        """Test is_valid property."""
        valid = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
        if not valid.is_valid:
            return False

        empty_bids = OrderBookSnapshot.from_raw("T/U", [], [(101, 10)])
        if empty_bids.is_valid:
            return False

        empty_asks = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [])
        if empty_asks.is_valid:
            return False

        empty_both = OrderBookSnapshot.from_raw("T/U", [], [])
        if empty_both.is_valid:
            return False

        return True

    @staticmethod
    def test_snapshot_is_crossed() -> bool:
        """Test is_crossed property."""
        normal = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
        if normal.is_crossed:
            return False

        crossed = OrderBookSnapshot.from_raw("T/U", [(102, 10)], [(101, 10)])
        if not crossed.is_crossed:
            return False

        equal = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(100, 10)])
        if not equal.is_crossed:
            return False

        return True

    @staticmethod
    def test_snapshot_is_stale() -> bool:
        """Test is_stale method."""
        fresh = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
        if fresh.is_stale(5000):
            return False

        old_ts = _now_ms() - 10000
        stale = OrderBookSnapshot.from_raw(
            "T/U", [(100, 10)], [(101, 10)], timestamp=old_ts
        )
        if not stale.is_stale(5000):
            return False

        return True

    @staticmethod
    def test_snapshot_age_ms() -> bool:
        """Test age_ms method."""
        snap = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
        age = snap.age_ms()

        if age < 0:
            return False
        if age > 1000:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # ORDER BOOK ANALYZER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_analyzer_creation() -> bool:
        """Test OrderBookAnalyzer creation."""
        analyzer = OrderBookAnalyzer(max_snapshots=50)

        if not analyzer.is_empty:
            return False
        if analyzer.count != 0:
            return False
        if analyzer.current is not None:
            return False

        return True

    @staticmethod
    def test_analyzer_add_snapshot() -> bool:
        """Test adding snapshots to analyzer."""
        analyzer = OrderBookAnalyzer(max_snapshots=10)

        for i in range(5):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100 + i, 10)],
                [(101 + i, 10)],
                sequence=i + 1
            )
            analyzer.add_snapshot(snap)

        if analyzer.count != 5:
            return False
        if analyzer.is_empty:
            return False
        if analyzer.current is None:
            return False
        if analyzer.total_updates != 5:
            return False

        return True

    @staticmethod
    def test_analyzer_max_snapshots() -> bool:
        """Test max snapshots limit."""
        analyzer = OrderBookAnalyzer(max_snapshots=5)

        for i in range(10):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101, 10)],
                sequence=i + 1
            )
            analyzer.add_snapshot(snap)

        if analyzer.count != 5:
            return False
        if analyzer.total_updates != 10:
            return False

        return True

    @staticmethod
    def test_analyzer_sequence_gaps() -> bool:
        """Test sequence gap detection."""
        analyzer = OrderBookAnalyzer()

        for seq in [1, 2, 5, 6, 10]:
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101, 10)],
                sequence=seq
            )
            analyzer.add_snapshot(snap)

        if analyzer.sequence_gaps != 5:
            return False

        return True

    @staticmethod
    def test_analyzer_get_recent() -> bool:
        """Test get_recent method."""
        analyzer = OrderBookAnalyzer(max_snapshots=100)

        for i in range(50):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100 + i * 0.01, 10)],
                [(101, 10)]
            )
            analyzer.add_snapshot(snap)

        recent = analyzer.get_recent(10)
        if len(recent) != 10:
            return False

        all_snaps = analyzer.get_recent(100)
        if len(all_snaps) != 50:
            return False

        return True

    @staticmethod
    def test_analyzer_get_snapshot_at() -> bool:
        """Test index-based snapshot lookup."""
        analyzer = OrderBookAnalyzer(max_snapshots=10)

        for i in range(5):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100 + i, 10)],
                [(101 + i, 10)]
            )
            analyzer.add_snapshot(snap)

        snap_0 = analyzer.get_snapshot_at(0)
        if snap_0 is None or snap_0.best_bid != 100:
            return False

        snap_4 = analyzer.get_snapshot_at(4)
        if snap_4 is None or snap_4.best_bid != 104:
            return False

        snap_out = analyzer.get_snapshot_at(10)
        if snap_out is not None:
            return False

        return True

    @staticmethod
    def test_analyzer_time_lookup() -> bool:
        """Test time-based snapshot lookup."""
        analyzer = OrderBookAnalyzer(max_snapshots=100)

        times = [1000, 2000, 3000, 4000, 5000]
        for t in times:
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101, 10)],
                timestamp=t
            )
            analyzer.add_snapshot(snap)

        result = analyzer.get_snapshot_at_time(3000)
        if result is None or result.timestamp != 3000:
            return False

        result = analyzer.get_snapshot_at_time(2500)
        if result is None or result.timestamp != 2000:
            return False

        result = analyzer.get_snapshot_at_time(500)
        if result is not None:
            return False

        result = analyzer.get_snapshot_at_time(6000)
        if result is None or result.timestamp != 5000:
            return False

        return True

    @staticmethod
    def test_analyzer_spread_metrics() -> bool:
        """Test spread analysis methods."""
        analyzer = OrderBookAnalyzer()

        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101 + i * 0.1, 10)]
            )
            analyzer.add_snapshot(snap)

        avg_spread = analyzer.average_spread_pct()
        if avg_spread <= 0:
            return False

        avg_spread_bps = analyzer.average_spread_bps()
        if avg_spread_bps <= 0:
            return False

        spread_vol = analyzer.spread_volatility()
        if spread_vol < 0:
            return False

        return True

    @staticmethod
    def test_analyzer_is_spread_acceptable() -> bool:
        """Test spread acceptability check."""
        analyzer = OrderBookAnalyzer()

        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10)],
            [(101, 10)]
        )
        analyzer.add_snapshot(snap)

        if not analyzer.is_spread_acceptable(max_spread_pct=2.0):
            return False

        if analyzer.is_spread_acceptable(max_spread_pct=0.5):
            return False

        return True

    @staticmethod
    def test_analyzer_imbalance_metrics() -> bool:
        """Test imbalance analysis methods."""
        analyzer = OrderBookAnalyzer()

        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10 + i * 2)],
                [(101, 10)]
            )
            analyzer.add_snapshot(snap)

        avg_imb = analyzer.average_imbalance()
        if avg_imb <= 0:
            return False

        trend = analyzer.imbalance_trend()
        if trend <= 0:
            return False

        return True

    @staticmethod
    def test_analyzer_imbalance_acceleration() -> bool:
        """Test imbalance acceleration calculation."""
        analyzer = OrderBookAnalyzer()

        for i in range(30):
            if i < 15:
                bid_size = 10 + i * 2
            else:
                bid_size = 38 - (i - 15) * 2

            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, bid_size)],
                [(101, 10)]
            )
            analyzer.add_snapshot(snap)

        accel = analyzer.imbalance_acceleration(20)

        if not isinstance(accel, float):
            return False

        return True

    @staticmethod
    def test_analyzer_bullish_bearish() -> bool:
        """Test bullish/bearish detection."""
        bullish_analyzer = OrderBookAnalyzer()
        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 50 + i * 5)],
                [(101, 10)]
            )
            bullish_analyzer.add_snapshot(snap)

        if not bullish_analyzer.is_bullish():
            return False

        bearish_analyzer = OrderBookAnalyzer()
        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101, 50 + i * 5)]
            )
            bearish_analyzer.add_snapshot(snap)

        if not bearish_analyzer.is_bearish():
            return False

        return True

    @staticmethod
    def test_analyzer_signal_strength() -> bool:
        """Test signal strength calculation."""
        analyzer = OrderBookAnalyzer()

        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 30)],
                [(101, 30)]
            )
            analyzer.add_snapshot(snap)

        signal = analyzer.get_signal_strength()
        if signal < -1.0 or signal > 1.0:
            return False

        return True

    @staticmethod
    def test_analyzer_statistics() -> bool:
        """Test get_statistics method."""
        analyzer = OrderBookAnalyzer()

        for i in range(20):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10 + i)],
                [(101, 10)]
            )
            analyzer.add_snapshot(snap)

        stats = analyzer.get_statistics()

        required_keys = [
            "mid_price", "spread", "spread_pct", "spread_bps",
            "imbalance", "depth_imbalance_10", "avg_spread_pct",
            "avg_imbalance", "imbalance_trend", "signal_strength",
            "microprice", "weighted_mid", "bid_volume_10", "ask_volume_10",
            "snapshot_count", "sequence_gaps", "data_fresh"
        ]

        for key in required_keys:
            if key not in stats:
                return False

        stats2 = analyzer.get_statistics()
        if stats != stats2:
            return False

        return True

    @staticmethod
    def test_analyzer_wall_detection() -> bool:
        """Test wall detection through analyzer."""
        analyzer = OrderBookAnalyzer()

        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 100), (98, 10)],
            [(101, 10), (102, 100), (103, 10)]
        )
        analyzer.add_snapshot(snap)

        has_bid_wall = analyzer.has_bid_wall_near(99, tolerance_pct=2.0, threshold_mult=2.5)
        if not has_bid_wall:
            return False

        has_ask_wall = analyzer.has_ask_wall_near(102, tolerance_pct=2.0, threshold_mult=2.5)
        if not has_ask_wall:
            return False

        return True

    @staticmethod
    def test_analyzer_clear() -> bool:
        """Test analyzer clear method."""
        analyzer = OrderBookAnalyzer()

        for i in range(10):
            snap = OrderBookSnapshot.from_raw(
                "TEST/USDT",
                [(100, 10)],
                [(101, 10)],
                sequence=i + 1
            )
            analyzer.add_snapshot(snap)

        analyzer.clear()

        if not analyzer.is_empty:
            return False
        if analyzer.count != 0:
            return False
        if analyzer.total_updates != 0:
            return False
        if analyzer.sequence_gaps != 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # TICK CLASSIFIER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_tick_classifier() -> bool:
        """Test tick classification."""
        classifier = TickClassifier()

        prices = [100.0, 100.1, 100.2, 100.2, 100.1, 100.0]
        expected = [
            TradeSide.UNKNOWN,
            TradeSide.BUY,
            TradeSide.BUY,
            TradeSide.BUY,
            TradeSide.SELL,
            TradeSide.SELL,
        ]

        results = classifier.classify_batch(prices)

        for i, (result, expect) in enumerate(zip(results, expected)):
            if result != expect:
                return False

        return True

    @staticmethod
    def test_tick_classifier_reset() -> bool:
        """Test tick classifier reset."""
        classifier = TickClassifier()

        classifier.classify(100.0)
        classifier.classify(100.1)

        classifier.reset()

        result = classifier.classify(100.0)
        if result != TradeSide.UNKNOWN:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # ORDER FLOW TRACKER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_order_flow_tracker() -> bool:
        """Test order flow tracking."""
        tracker = OrderFlowTracker(large_trade_threshold=5.0)

        tracker.process_trade(100.0, 1.0)
        tracker.process_trade(100.1, 2.0)
        tracker.process_trade(100.2, 3.0)
        tracker.process_trade(100.1, 1.5)
        tracker.process_trade(100.0, 10.0)

        metrics = tracker.get_metrics()

        if metrics.trade_count != 4:
            return False
        if metrics.unknown_count != 1:
            return False
        if metrics.large_sell_count != 1:
            return False
        if metrics.total_trade_count != 5:
            return False
        if not (-1.0 <= metrics.ofi <= 1.0):
            return False

        return True

    @staticmethod
    def test_order_flow_batch() -> bool:
        """Test batch trade processing."""
        tracker = OrderFlowTracker()

        trades = [
            (100.0, 1.0),
            (100.1, 2.0),
            (100.2, 3.0),
        ]

        metrics = tracker.process_batch(trades)

        if metrics.total_trade_count != 3:
            return False

        return True

    @staticmethod
    def test_order_flow_delta() -> bool:
        """Test cumulative delta tracking."""
        tracker = OrderFlowTracker()

        tracker.process_trade(100.0, 1.0)
        tracker.process_trade(100.1, 2.0)
        tracker.process_trade(100.0, 1.0)

        metrics = tracker.get_metrics()

        recent_delta = tracker.get_recent_delta()
        momentum = tracker.get_delta_momentum()

        if not isinstance(recent_delta, float):
            return False
        if not isinstance(momentum, float):
            return False

        return True

    @staticmethod
    def test_order_flow_reset() -> bool:
        """Test order flow reset."""
        tracker = OrderFlowTracker()

        tracker.process_trade(100.0, 1.0)
        tracker.process_trade(100.1, 2.0)

        tracker.reset()

        metrics = tracker.get_metrics()
        if metrics.total_trade_count != 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # SLIPPAGE ESTIMATOR TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_slippage_estimator_estimate() -> bool:
        """Test slippage estimation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 10), (102, 20)]
        )

        price, slippage, fill_prob = SlippageEstimator.estimate(
            snap, 5, "buy", aggression=1.0
        )

        if price <= 0:
            return False
        if slippage < 0:
            return False
        if fill_prob < 0 or fill_prob > 1:
            return False

        return True

    @staticmethod
    def test_slippage_estimator_fixed() -> bool:
        """Test fixed slippage calculation."""
        buy_price = SlippageEstimator.estimate_fixed(100.0, "buy", 5.0)
        if abs(buy_price - 100.05) > 0.001:
            return False

        sell_price = SlippageEstimator.estimate_fixed(100.0, "sell", 5.0)
        if abs(sell_price - 99.95) > 0.001:
            return False

        return True

    @staticmethod
    def test_slippage_estimator_fill() -> bool:
        """Test fill simulation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 10), (102, 20)]
        )

        result = SlippageEstimator.simulate_fill(
            snap, 5, "buy", max_slippage_bps=50.0
        )

        if result is None:
            return False

        fill_price, filled_size = result
        if fill_price <= 0:
            return False
        if filled_size <= 0:
            return False

        return True

    @staticmethod
    def test_slippage_estimator_impact_cost() -> bool:
        """Test market impact cost calculation."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 10), (102, 20)]
        )

        costs = SlippageEstimator.calculate_market_impact_cost(snap, 15, "buy")

        required_keys = [
            "mid_price", "fill_price", "half_spread_cost",
            "price_impact", "total_slippage", "slippage_bps", "total_cost_pct"
        ]

        for key in required_keys:
            if key not in costs:
                return False

        return True

    # ─────────────────────────────────────────────────────────
    # EXECUTION SIMULATOR TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_execution_simulator_entry() -> bool:
        """Test execution simulator entry."""
        sim = BacktestExecutionSimulator(
            slippage_model="fixed",
            fixed_slippage_bps=2.0,
            fee_bps=4.0
        )

        entry = sim.simulate_entry(100.0, 1.0, "buy")

        if entry["fill_price"] <= 100.0:
            return False
        if entry["fee"] <= 0:
            return False
        if entry["total_outlay"] <= 0:
            return False
        if entry["side"] != "buy":
            return False

        return True

    @staticmethod
    def test_execution_simulator_exit() -> bool:
        """Test execution simulator exit."""
        sim = BacktestExecutionSimulator(
            slippage_model="fixed",
            fixed_slippage_bps=2.0,
            fee_bps=4.0
        )

        entry = sim.simulate_entry(100.0, 1.0, "buy")
        result = sim.simulate_exit(entry, 105.0)

        if "net_pnl" not in result:
            return False
        if "gross_pnl" not in result:
            return False
        if "fees" not in result:
            return False
        if result["gross_pnl"] <= 0:
            return False

        return True

    @staticmethod
    def test_execution_simulator_round_trip() -> bool:
        """Test complete round trip simulation."""
        sim = BacktestExecutionSimulator(
            slippage_model="none",
            fee_bps=0.0
        )

        result = sim.simulate_round_trip(100.0, 110.0, 1.0, "buy")

        expected_pnl = 10.0
        if abs(result["net_pnl"] - expected_pnl) > 0.01:
            return False

        return True

    @staticmethod
    def test_execution_simulator_realistic() -> bool:
        """Test realistic slippage model."""
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10), (99, 20)],
            [(101, 10), (102, 20)]
        )

        sim = BacktestExecutionSimulator(
            slippage_model="realistic",
            fee_bps=4.0
        )

        entry = sim.simulate_entry(100.5, 1.0, "buy", orderbook=snap)

        if entry["fill_price"] <= 0:
            return False

        return True

    @staticmethod
    def test_execution_simulator_statistics() -> bool:
        """Test execution statistics."""
        sim = BacktestExecutionSimulator(slippage_model="fixed", fee_bps=4.0)

        for i in range(5):
            sim.simulate_entry(100.0 + i, 1.0, "buy")

        stats = sim.get_statistics()

        if stats["trade_count"] != 5:
            return False
        if stats["total_fees"] <= 0:
            return False

        sim.reset_statistics()
        stats = sim.get_statistics()
        if stats["trade_count"] != 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # SYNTHETIC ORDER BOOK TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_synthetic_from_ticks() -> bool:
        """Test synthetic order book from ticks."""
        ticks = [(i * 1000, 100.0 + (i % 10) * 0.01) for i in range(100)]

        snap1 = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)
        snap2 = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)

        if snap1 is None or snap2 is None:
            return False

        if not snap1.is_valid:
            return False

        if snap1.best_bid != snap2.best_bid:
            return False

        return True

    @staticmethod
    def test_synthetic_from_candle() -> bool:
        """Test synthetic order book from candle."""
        snap = SyntheticOrderBook.from_candle(
            timestamp=_now_ms(),
            open_price=100.0,
            high_price=105.0,
            low_price=95.0,
            close_price=102.0,
            volume=1000,
            levels=10,
            random_seed=42
        )

        if not snap.is_valid:
            return False

        if len(snap.bids) != 10:
            return False
        if len(snap.asks) != 10:
            return False

        return True

    @staticmethod
    def test_synthetic_generate_analyzer() -> bool:
        """Test generating analyzer from ticks."""
        ticks = [(i * 1000, 100.0 + (i % 10) * 0.01) for i in range(100)]

        analyzer = SyntheticOrderBook.generate_analyzer(
            ticks, sample_every=10
        )

        if analyzer is None:
            return False

        if analyzer.count != 10:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # DATA VALIDATOR TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_data_validator_valid() -> bool:
        """Test data validator with valid data."""
        validator = DataValidator()

        snap = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
        is_valid, warnings = validator.validate(snap)

        if not is_valid:
            return False

        return True

    @staticmethod
    def test_data_validator_crossed() -> bool:
        """Test data validator with crossed book."""
        validator = DataValidator()

        snap = OrderBookSnapshot.from_raw("T/U", [(102, 10)], [(101, 10)])
        is_valid, warnings = validator.validate(snap)

        if is_valid:
            return False

        return True

    @staticmethod
    def test_data_validator_empty() -> bool:
        """Test data validator with empty book."""
        validator = DataValidator()

        snap = OrderBookSnapshot.from_raw("T/U", [], [])
        is_valid, warnings = validator.validate(snap)

        if is_valid:
            return False

        return True

    @staticmethod
    def test_data_validator_reset() -> bool:
        """Test data validator reset."""
        validator = DataValidator(max_symbols=10)

        for i in range(5):
            snap = OrderBookSnapshot.from_raw(
                f"SYM{i}/USDT", [(100, 10)], [(101, 10)]
            )
            validator.validate(snap)

        stats = validator.get_stats()
        if stats["tracked_symbols"] != 5:
            return False

        validator.reset()
        stats = validator.get_stats()
        if stats["tracked_symbols"] != 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # DELTA HANDLER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_delta_handler() -> bool:
        """Test order book delta handling."""
        handler = OrderBookDeltaHandler("TEST/USDT", max_levels=10)

        handler.apply_snapshot(
            bids=[(100, 10), (99, 20)],
            asks=[(101, 15), (102, 25)],
            update_id=1
        )

        snap = handler.get_snapshot()
        if snap is None:
            return False
        if snap.best_bid != 100:
            return False

        snap2 = handler.apply_delta(
            bids=[(100, 0), (98, 30)],
            asks=[],
            update_id=2
        )
        if snap2 is None:
            return False
        if snap2.best_bid != 99:
            return False

        snap3 = handler.apply_delta(bids=[(97, 10)], asks=[], update_id=1)
        if snap3 is not None:
            return False

        handler.reset()
        if handler.get_snapshot() is not None:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # ALERT MANAGER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_alert_manager() -> bool:
        """Test alert management."""
        alerts = AlertManager(throttle_seconds=1)
        received = []

        def handler(level, message, context):
            received.append((level, message))

        alerts.add_handler(handler)

        result1 = alerts.info("Test message")
        if not result1:
            return False

        result2 = alerts.warning("Warning", throttle_key="key1")
        if not result2:
            return False

        result3 = alerts.warning("Warning", throttle_key="key1")
        if result3:
            return False

        if len(received) != 2:
            return False

        return True

    @staticmethod
    def test_alert_manager_critical() -> bool:
        """Test critical alerts."""
        alerts = AlertManager()
        received = []

        alerts.add_handler(lambda l, m, c: received.append(l))

        alerts.critical("Critical issue")

        if len(received) != 1:
            return False
        if received[0] != "CRITICAL":
            return False

        return True

    @staticmethod
    def test_alert_manager_clear_throttle() -> bool:
        """Test throttle clearing."""
        alerts = AlertManager(throttle_seconds=60)

        alerts.warning("Test", throttle_key="t1")
        result = alerts.warning("Test", throttle_key="t1")
        if result:
            return False

        alerts.clear_throttle()
        result = alerts.warning("Test", throttle_key="t1")
        if not result:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # STATE MANAGER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_state_manager() -> bool:
        """Test state persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = os.path.join(tmpdir, "test_state.json")

            s1 = StateManager(sf)
            s1.set("key1", "value1")
            s1.set("key2", 42)
            s1.update_positions({"BTC/USDT": 0.5})

            s2 = StateManager(sf)

            if s2.get("key1") != "value1":
                return False
            if s2.get("key2") != 42:
                return False

            positions = s2.get_positions()
            if positions.get("BTC/USDT") != 0.5:
                return False

        return True

    @staticmethod
    def test_state_manager_running() -> bool:
        """Test running state tracking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = os.path.join(tmpdir, "state.json")

            s1 = StateManager(sf)
            s1.set_running(True)

            s2 = StateManager(sf)
            if not s2.was_running():
                return False

        return True

    @staticmethod
    def test_state_manager_clear() -> bool:
        """Test state clearing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = os.path.join(tmpdir, "state.json")

            s1 = StateManager(sf)
            s1.set("key", "value")
            s1.clear()

            if s1.get("key") is not None:
                return False

        return True

    # ─────────────────────────────────────────────────────────
    # THROTTLED CALLBACK TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_throttled_callback() -> bool:
        """Test callback throttling."""
        calls = []

        def callback(data):
            calls.append(data)

        tc = ThrottledCallback(callback, min_interval_ms=100)

        for i in range(10):
            tc(i)

        if len(calls) != 1:
            return False

        time.sleep(0.15)
        tc(99)

        if len(calls) != 2:
            return False

        return True

    @staticmethod
    def test_throttled_callback_flush() -> bool:
        """Test throttled callback flush."""
        calls = []

        def callback(data):
            calls.append(data)

        tc = ThrottledCallback(callback, min_interval_ms=1000)

        tc(1)
        tc(2)
        tc(3)

        if len(calls) != 1:
            return False

        tc.flush()

        if len(calls) != 2:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # STREAM HEALTH TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_stream_health() -> bool:
        """Test stream health tracking."""
        h = StreamHealth()

        if h.is_healthy:
            return False

        h.connected = True
        h.last_update_ms = _now_ms()
        h.latency_ms = 50

        if not h.is_healthy:
            return False

        for _ in range(15):
            h.record_error("test error")

        if h.is_healthy:
            return False
        if h.error_count != 15:
            return False

        h.reset_errors()

        if not h.is_healthy:
            return False
        if h.error_count != 0:
            return False

        return True

    @staticmethod
    def test_stream_health_reconnect() -> bool:
        """Test reconnect tracking."""
        h = StreamHealth()

        h.record_reconnect()
        h.record_reconnect()

        if h.reconnect_count != 2:
            return False

        return True

    @staticmethod
    def test_stream_health_to_dict() -> bool:
        """Test health serialization."""
        h = StreamHealth()
        h.connected = True
        h.last_update_ms = _now_ms()
        h.latency_ms = 100

        d = h.to_dict()

        required_keys = [
            "connected", "age_ms", "updates_per_second",
            "sequence_gaps", "reconnect_count", "latency_ms",
            "error_count", "last_error", "is_healthy"
        ]

        for key in required_keys:
            if key not in d:
                return False

        return True

    # ─────────────────────────────────────────────────────────
    # BACKTEST PROVIDER TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_backtest_provider() -> bool:
        """Test backtest data provider."""
        provider = BacktestOrderBookProvider()

        candles = [
            {
                "timestamp": i * 60000,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000
            }
            for i in range(50)
        ]

        provider.load_from_candles("BTC/USDT", candles)

        analyzer = provider.get_analyzer("BTC/USDT")
        if analyzer is None:
            return False
        if analyzer.count != 50:
            return False

        snap = provider.get_snapshot("BTC/USDT")
        if snap is None:
            return False

        snap_at_time = provider.get_snapshot_at_time("BTC/USDT", 25 * 60000)
        if snap_at_time is None:
            return False

        symbols = provider.get_all_symbols()
        if "BTC/USDT" not in symbols:
            return False

        provider.clear("BTC/USDT")
        if provider.get_analyzer("BTC/USDT") is not None:
            return False

        return True

    @staticmethod
    def test_backtest_provider_ticks() -> bool:
        """Test loading ticks into backtest provider."""
        provider = BacktestOrderBookProvider()

        ticks = [(i * 1000, 100.0 + i * 0.01) for i in range(100)]
        provider.load_from_ticks("ETH/USDT", ticks, sample_every=10)

        analyzer = provider.get_analyzer("ETH/USDT")
        if analyzer is None:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # SYMBOL VALIDATION TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_symbol_validation() -> bool:
        """Test symbol format validation."""
        valid_symbols = ["BTC/USDT", "ETH/BTC", "1INCH/USDT", "XRP/USDT"]
        invalid_symbols = ["btc/usdt", "BTC-USDT", "BTC", "/USDT", "BTC/"]

        for s in valid_symbols:
            if not OrderBookConstants.SYMBOL_PATTERN.match(s):
                return False

        for s in invalid_symbols:
            if OrderBookConstants.SYMBOL_PATTERN.match(s):
                return False

        return True

    # ─────────────────────────────────────────────────────────
    # THREAD SAFETY TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_thread_safety() -> bool:
        """Test thread safety of analyzer."""
        analyzer = OrderBookAnalyzer()
        errors = []
        stop_event = threading.Event()

        def writer():
            i = 0
            while not stop_event.is_set():
                try:
                    snap = OrderBookSnapshot.from_raw(
                        "T/U",
                        [(100 + i * 0.001, 10)],
                        [(101, 10)],
                        sequence=i
                    )
                    analyzer.add_snapshot(snap)
                    i += 1
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(str(e))

        def reader():
            while not stop_event.is_set():
                try:
                    current = analyzer.current
                    stats = analyzer.get_statistics()
                    recent = analyzer.get_recent(10)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(str(e))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()

        time.sleep(0.5)
        stop_event.set()

        for t in threads:
            t.join()

        if len(errors) > 0:
            return False

        return True

    # ─────────────────────────────────────────────────────────
    # EDGE CASE TESTS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def test_empty_snapshots() -> bool:
        """Test handling of empty snapshots."""
        empty = OrderBookSnapshot.from_raw("T/U", [], [])

        if empty.is_valid:
            return False
        if empty.mid_price != 0.0:
            return False
        if empty.spread != 0.0:
            return False
        if empty.imbalance != 0.0:
            return False

        return True

    @staticmethod
    def test_zero_size_filtering() -> bool:
        """Test that zero-size levels are filtered."""
        snap = OrderBookSnapshot.from_raw(
            "T/U",
            [(100, 10), (99, 0), (98, 20)],
            [(101, 15), (102, 0), (103, 25)]
        )

        if len(snap.bids) != 2:
            return False
        if len(snap.asks) != 2:
            return False

        return True


def run_unit_tests(suite: TestSuite) -> bool:
    """Run all unit tests."""
    print_header("UNIT TESTS (No Network Required)")

    tests = [
        # OrderBookLevel
        ("OrderBookLevel", UnitTests.test_order_book_level),

        # OrderBookSnapshot Creation
        ("Snapshot from_raw", UnitTests.test_snapshot_from_raw),
        ("Snapshot from_sorted", UnitTests.test_snapshot_from_sorted),
        ("Snapshot from_binance_fast", UnitTests.test_snapshot_from_binance_fast),
        ("Snapshot Serialization", UnitTests.test_snapshot_serialization),

        # OrderBookSnapshot Metrics
        ("Snapshot Basic Metrics", UnitTests.test_snapshot_basic_metrics),
        ("Snapshot Volume Metrics", UnitTests.test_snapshot_volume_metrics),
        ("Snapshot Depth Imbalance", UnitTests.test_snapshot_depth_imbalance),
        ("Snapshot Value Imbalance", UnitTests.test_snapshot_value_imbalance),
        ("Snapshot VWAP", UnitTests.test_snapshot_vwap),
        ("Snapshot Microprice", UnitTests.test_snapshot_microprice),
        ("Snapshot Weighted Mid", UnitTests.test_snapshot_weighted_mid),

        # Market Impact
        ("Market Impact Small", UnitTests.test_market_impact_small),
        ("Market Impact Large", UnitTests.test_market_impact_large),
        ("Market Impact Zero", UnitTests.test_market_impact_zero),

        # Wall Detection
        ("Wall Detection", UnitTests.test_wall_detection),
        ("Nearest Walls", UnitTests.test_nearest_walls),

        # Liquidity
        ("Liquidity at Price", UnitTests.test_liquidity_at_price),
        ("Depth at Distance", UnitTests.test_depth_at_distance),

        # Validation
        ("Snapshot is_valid", UnitTests.test_snapshot_is_valid),
        ("Snapshot is_crossed", UnitTests.test_snapshot_is_crossed),
        ("Snapshot is_stale", UnitTests.test_snapshot_is_stale),
        ("Snapshot age_ms", UnitTests.test_snapshot_age_ms),

        # OrderBookAnalyzer
        ("Analyzer Creation", UnitTests.test_analyzer_creation),
        ("Analyzer Add Snapshot", UnitTests.test_analyzer_add_snapshot),
        ("Analyzer Max Snapshots", UnitTests.test_analyzer_max_snapshots),
        ("Analyzer Sequence Gaps", UnitTests.test_analyzer_sequence_gaps),
        ("Analyzer get_recent", UnitTests.test_analyzer_get_recent),
        ("Analyzer get_snapshot_at", UnitTests.test_analyzer_get_snapshot_at),
        ("Analyzer Time Lookup", UnitTests.test_analyzer_time_lookup),
        ("Analyzer Spread Metrics", UnitTests.test_analyzer_spread_metrics),
        ("Analyzer is_spread_acceptable", UnitTests.test_analyzer_is_spread_acceptable),
        ("Analyzer Imbalance Metrics", UnitTests.test_analyzer_imbalance_metrics),
        ("Analyzer Imbalance Acceleration", UnitTests.test_analyzer_imbalance_acceleration),
        ("Analyzer Bullish/Bearish", UnitTests.test_analyzer_bullish_bearish),
        ("Analyzer Signal Strength", UnitTests.test_analyzer_signal_strength),
        ("Analyzer Statistics", UnitTests.test_analyzer_statistics),
        ("Analyzer Wall Detection", UnitTests.test_analyzer_wall_detection),
        ("Analyzer Clear", UnitTests.test_analyzer_clear),

        # TickClassifier
        ("Tick Classifier", UnitTests.test_tick_classifier),
        ("Tick Classifier Reset", UnitTests.test_tick_classifier_reset),

        # OrderFlowTracker
        ("Order Flow Tracker", UnitTests.test_order_flow_tracker),
        ("Order Flow Batch", UnitTests.test_order_flow_batch),
        ("Order Flow Delta", UnitTests.test_order_flow_delta),
        ("Order Flow Reset", UnitTests.test_order_flow_reset),

        # SlippageEstimator
        ("Slippage Estimate", UnitTests.test_slippage_estimator_estimate),
        ("Slippage Fixed", UnitTests.test_slippage_estimator_fixed),
        ("Slippage Fill", UnitTests.test_slippage_estimator_fill),
        ("Slippage Impact Cost", UnitTests.test_slippage_estimator_impact_cost),

        # BacktestExecutionSimulator
        ("Execution Entry", UnitTests.test_execution_simulator_entry),
        ("Execution Exit", UnitTests.test_execution_simulator_exit),
        ("Execution Round Trip", UnitTests.test_execution_simulator_round_trip),
        ("Execution Realistic", UnitTests.test_execution_simulator_realistic),
        ("Execution Statistics", UnitTests.test_execution_simulator_statistics),

        # SyntheticOrderBook
        ("Synthetic from Ticks", UnitTests.test_synthetic_from_ticks),
        ("Synthetic from Candle", UnitTests.test_synthetic_from_candle),
        ("Synthetic Analyzer", UnitTests.test_synthetic_generate_analyzer),

        # DataValidator
        ("Data Validator Valid", UnitTests.test_data_validator_valid),
        ("Data Validator Crossed", UnitTests.test_data_validator_crossed),
        ("Data Validator Empty", UnitTests.test_data_validator_empty),
        ("Data Validator Reset", UnitTests.test_data_validator_reset),

        # DeltaHandler
        ("Delta Handler", UnitTests.test_delta_handler),

        # AlertManager
        ("Alert Manager", UnitTests.test_alert_manager),
        ("Alert Manager Critical", UnitTests.test_alert_manager_critical),
        ("Alert Clear Throttle", UnitTests.test_alert_manager_clear_throttle),

        # StateManager
        ("State Manager", UnitTests.test_state_manager),
        ("State Running", UnitTests.test_state_manager_running),
        ("State Clear", UnitTests.test_state_manager_clear),

        # ThrottledCallback
        ("Throttled Callback", UnitTests.test_throttled_callback),
        ("Throttled Flush", UnitTests.test_throttled_callback_flush),

        # StreamHealth
        ("Stream Health", UnitTests.test_stream_health),
        ("Stream Reconnect", UnitTests.test_stream_health_reconnect),
        ("Stream to_dict", UnitTests.test_stream_health_to_dict),

        # BacktestOrderBookProvider
        ("Backtest Provider", UnitTests.test_backtest_provider),
        ("Backtest Ticks", UnitTests.test_backtest_provider_ticks),

        # Symbol Validation
        ("Symbol Validation", UnitTests.test_symbol_validation),

        # Thread Safety
        ("Thread Safety", UnitTests.test_thread_safety),

        # Edge Cases
        ("Empty Snapshots", UnitTests.test_empty_snapshots),
        ("Zero Size Filtering", UnitTests.test_zero_size_filtering),
    ]

    all_passed = True
    for name, test_fn in tests:
        if not suite.run_test(name, test_fn):
            all_passed = False

    return all_passed


# ============================================================
# BENCHMARK TESTS
# ============================================================

class Benchmarks:
    """Performance benchmarks."""

    @staticmethod
    def parsing(suite: TestSuite) -> None:
        """Benchmark parsing speed: from_raw vs from_binance_fast."""
        print_subheader("PARSING BENCHMARK")

        bids, asks = generate_binance_data(20)
        timestamp = _now_ms()
        symbol = "BTC/USDT"

        bids_tuples = [(float(p), float(s)) for p, s in bids]
        asks_tuples = [(float(p), float(s)) for p, s in asks]

        def test_from_raw():
            return OrderBookSnapshot.from_raw(
                symbol, bids_tuples, asks_tuples, timestamp
            )

        def test_from_binance_fast():
            return OrderBookSnapshot.from_binance_fast(
                symbol, bids, asks, timestamp, 12345
            )

        print("\n  Testing with 20 levels, 10,000 iterations...")

        result_raw = suite.benchmark("from_raw", test_from_raw, 10000)
        result_fast = suite.benchmark("from_binance_fast", test_from_binance_fast, 10000)

        suite.print_benchmark(result_raw)
        suite.print_benchmark(result_fast)
        suite.compare_benchmarks("from_raw", result_raw, "from_binance_fast", result_fast)

    @staticmethod
    def lookup(suite: TestSuite) -> None:
        """Benchmark symbol lookup methods."""
        print_subheader("SYMBOL LOOKUP BENCHMARK")

        symbols = [f"SYM{i}/USDT" for i in range(100)]

        stream_to_symbol = {}
        symbol_to_ws = {}

        for symbol in symbols:
            clean_lower = symbol.replace("/", "").lower()
            clean_upper = clean_lower.upper()
            stream_key = f"{clean_lower}@depth20@100ms"
            stream_to_symbol[stream_key] = symbol
            symbol_to_ws[clean_upper] = symbol

        test_streams = [f"sym{i}usdt@depth20@100ms" for i in range(100)]

        def lookup_old():
            results = []
            for stream in test_streams:
                ws_symbol = stream.split("@")[0].upper()
                symbol = symbol_to_ws.get(ws_symbol)
                results.append(symbol)
            return results

        def lookup_new():
            results = []
            for stream in test_streams:
                symbol = stream_to_symbol.get(stream)
                results.append(symbol)
            return results

        print("\n  Testing 100 symbol lookups, 10,000 iterations...")

        result_old = suite.benchmark("String split+upper", lookup_old, 10000)
        result_new = suite.benchmark("Direct lookup", lookup_new, 10000)

        suite.print_benchmark(result_old)
        suite.print_benchmark(result_new)
        suite.compare_benchmarks("Old", result_old, "New", result_new)

    @staticmethod
    def get_recent(suite: TestSuite) -> None:
        """Benchmark get_recent implementations."""
        print_subheader("GET_RECENT BENCHMARK")

        bids, asks = generate_binance_data(20)
        timestamp = _now_ms()

        snapshots: deque = deque(maxlen=1000)
        for i in range(1000):
            snap = OrderBookSnapshot.from_binance_fast(
                "BTC/USDT", bids, asks, timestamp + i
            )
            snapshots.append(snap)

        def get_recent_old(n: int = 50):
            return list(snapshots)[-n:]

        def get_recent_new(n: int = 50):
            length = len(snapshots)
            if length <= n:
                return list(snapshots)
            start = length - n
            return list(islice(snapshots, start, None))

        print("\n  Testing get_recent(50) from 1000 snapshots...")

        result_old = suite.benchmark("list()[-n:]", get_recent_old, 10000)
        result_new = suite.benchmark("islice", get_recent_new, 10000)

        suite.print_benchmark(result_old)
        suite.print_benchmark(result_new)
        suite.compare_benchmarks("Old", result_old, "New", result_new)

    @staticmethod
    def pipeline(suite: TestSuite) -> None:
        """Benchmark full message processing pipeline."""
        print_subheader("FULL PIPELINE BENCHMARK")

        symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

        stream_to_symbol = {
            f"{s.lower().replace('/', '')}@depth20@100ms": s
            for s in symbols
        }
        symbol_to_ws = {
            s.replace("/", "").upper(): s
            for s in symbols
        }

        def generate_message(symbol: str) -> Dict[str, Any]:
            bids, asks = generate_binance_data(20)
            return {
                "stream": f"{symbol.lower().replace('/', '')}@depth20@100ms",
                "data": {
                    "bids": bids,
                    "asks": asks,
                    "E": _now_ms(),
                    "lastUpdateId": random.randint(1000000, 9999999),
                }
            }

        messages = [generate_message(random.choice(symbols)) for _ in range(100)]

        def process_old():
            results = []
            for msg in messages:
                stream = msg.get("stream", "")
                payload = msg.get("data", {})

                ws_symbol = stream.split("@")[0].upper()
                symbol = symbol_to_ws.get(ws_symbol)
                if not symbol:
                    continue

                bids = payload.get("bids", [])
                asks = payload.get("asks", [])

                bids_tuples = [(float(p), float(s)) for p, s in bids]
                asks_tuples = [(float(p), float(s)) for p, s in asks]

                snapshot = OrderBookSnapshot.from_raw(
                    symbol, bids_tuples, asks_tuples, payload.get("E", _now_ms())
                )
                results.append(snapshot)
            return results

        def process_new():
            results = []
            for msg in messages:
                stream = msg.get("stream", "")
                payload = msg.get("data", {})

                symbol = stream_to_symbol.get(stream)
                if not symbol:
                    continue

                snapshot = OrderBookSnapshot.from_binance_fast(
                    symbol,
                    payload.get("bids", []),
                    payload.get("asks", []),
                    payload.get("E", _now_ms()),
                    payload.get("lastUpdateId", 0)
                )
                results.append(snapshot)
            return results

        print("\n  Testing 100 messages per batch, 1,000 iterations...")

        result_old = suite.benchmark("Original pipeline", process_old, 1000, warmup=50)
        result_new = suite.benchmark("Optimized pipeline", process_new, 1000, warmup=50)

        suite.print_benchmark(result_old)
        suite.print_benchmark(result_new)
        suite.compare_benchmarks("Original", result_old, "Optimized", result_new)

        old_msg_per_sec = 100 * result_old.ops_per_sec
        new_msg_per_sec = 100 * result_new.ops_per_sec

        print(f"\n  📊 Throughput:")
        print(f"     Original:  {old_msg_per_sec:,.0f} messages/sec")
        print(f"     Optimized: {new_msg_per_sec:,.0f} messages/sec")

    @staticmethod
    def metrics(suite: TestSuite) -> None:
        """Benchmark metric calculations."""
        print_subheader("METRICS CALCULATION BENCHMARK")

        bids = [(100 - i * 0.1, 10 + i) for i in range(20)]
        asks = [(100.1 + i * 0.1, 10 + i) for i in range(20)]
        snap = OrderBookSnapshot.from_raw("BTC/USDT", bids, asks)

        def test_basic_metrics():
            return (
                snap.mid_price,
                snap.spread,
                snap.spread_bps,
                snap.imbalance
            )

        def test_vwap():
            return (
                snap.vwap_bid(10),
                snap.vwap_ask(10),
                snap.weighted_mid_price(10)
            )

        def test_market_impact():
            return snap.market_impact(50, "buy")

        def test_wall_detection():
            return snap.find_walls(threshold_mult=3.0)

        print("\n  Testing metric calculations, 10,000 iterations...")

        result_basic = suite.benchmark("Basic metrics", test_basic_metrics, 10000)
        result_vwap = suite.benchmark("VWAP calculations", test_vwap, 10000)
        result_impact = suite.benchmark("Market impact", test_market_impact, 10000)
        result_walls = suite.benchmark("Wall detection", test_wall_detection, 10000)

        suite.print_benchmark(result_basic)
        suite.print_benchmark(result_vwap)
        suite.print_benchmark(result_impact)
        suite.print_benchmark(result_walls)

    @staticmethod
    def analyzer(suite: TestSuite) -> None:
        """Benchmark analyzer operations."""
        print_subheader("ANALYZER BENCHMARK")

        analyzer = OrderBookAnalyzer(max_snapshots=1000)
        for i in range(1000):
            snap = OrderBookSnapshot.from_raw(
                "BTC/USDT",
                [(100 + random.uniform(-1, 1), 10)],
                [(101 + random.uniform(-1, 1), 10)],
                timestamp=i * 1000,
                sequence=i
            )
            analyzer.add_snapshot(snap)

        def test_get_statistics():
            return analyzer.get_statistics()

        def test_get_recent():
            return analyzer.get_recent(50)

        def test_imbalance_trend():
            return analyzer.imbalance_trend()

        def test_time_lookup():
            return analyzer.get_snapshot_at_time(500000)

        print("\n  Testing analyzer operations, 10,000 iterations...")

        result_stats = suite.benchmark("get_statistics", test_get_statistics, 10000)
        result_recent = suite.benchmark("get_recent(50)", test_get_recent, 10000)
        result_trend = suite.benchmark("imbalance_trend", test_imbalance_trend, 10000)
        result_lookup = suite.benchmark("time_lookup", test_time_lookup, 10000)

        suite.print_benchmark(result_stats)
        suite.print_benchmark(result_recent)
        suite.print_benchmark(result_trend)
        suite.print_benchmark(result_lookup)

    @staticmethod
    def memory(suite: TestSuite) -> None:
        """Benchmark memory usage."""
        print_subheader("MEMORY BENCHMARK")

        import tracemalloc

        has_slots_snapshot = hasattr(OrderBookSnapshot, '__slots__')
        has_slots_level = hasattr(OrderBookLevel, '__slots__')

        print(f"\n  __slots__ enabled:")
        print(f"     OrderBookSnapshot: {'✅' if has_slots_snapshot else '❌'}")
        print(f"     OrderBookLevel:    {'✅' if has_slots_level else '❌'}")

        gc.collect()
        tracemalloc.start()

        snapshots = []
        bids, asks = generate_binance_data(20)
        timestamp = _now_ms()

        for i in range(1000):
            snapshots.append(
                OrderBookSnapshot.from_binance_fast(
                    f"SYM{i}/USDT", bids, asks, timestamp
                )
            )

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"\n  Memory for 1,000 snapshots (20 levels each):")
        print(f"     Current: {current / 1024 / 1024:.2f} MB")
        print(f"     Peak:    {peak / 1024 / 1024:.2f} MB")
        print(f"     Per snapshot: {current / 1000 / 1024:.2f} KB")

        gc.collect()
        tracemalloc.start()

        analyzers = []
        for i in range(100):
            analyzer = OrderBookAnalyzer(max_snapshots=100)
            for j in range(100):
                snap = OrderBookSnapshot.from_binance_fast(
                    f"SYM{i}/USDT", bids, asks, timestamp + j
                )
                analyzer.add_snapshot(snap)
            analyzers.append(analyzer)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"\n  Memory for 100 analyzers (100 snapshots each):")
        print(f"     Current: {current / 1024 / 1024:.2f} MB")
        print(f"     Peak:    {peak / 1024 / 1024:.2f} MB")
        print(f"     Per analyzer: {current / 100 / 1024:.2f} KB")


def run_benchmarks(suite: TestSuite, specific: Optional[str] = None) -> None:
    """Run benchmark tests."""
    print_header("PERFORMANCE BENCHMARKS")

    benchmarks = {
        "parsing": Benchmarks.parsing,
        "lookup": Benchmarks.lookup,
        "recent": Benchmarks.get_recent,
        "pipeline": Benchmarks.pipeline,
        "metrics": Benchmarks.metrics,
        "analyzer": Benchmarks.analyzer,
        "memory": Benchmarks.memory,
    }

    if specific and specific in benchmarks:
        benchmarks[specific](suite)
    else:
        for name, bench_fn in benchmarks.items():
            bench_fn(suite)


# ============================================================
# LIVE TESTS
# ============================================================

class LiveTests:
    """Live WebSocket streaming tests."""

    @staticmethod
    def single_symbol(duration: int = 30) -> bool:
        """Test single symbol streaming."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed - skipping")
            return True

        print(f"\n  Testing BTC/USDT for {duration}s...")

        manager = OrderBookManager(store_history=False, enable_alerts=True)
        stats = {"count": 0, "spreads": [], "latencies": []}

        def on_update(snap: OrderBookSnapshot) -> None:
            stats["count"] += 1
            if snap.is_valid:
                stats["spreads"].append(snap.spread_bps)

            if stats["count"] % 20 == 0:
                h = manager.get_health("BTC/USDT")
                if h:
                    stats["latencies"].append(h.latency_ms)
                    print(
                        f"     [{stats['count']:4d}] "
                        f"Mid: ${snap.mid_price:,.2f} | "
                        f"Spread: {snap.spread_bps:.2f}bps | "
                        f"Imb: {snap.imbalance:+.3f} | "
                        f"Lat: {h.latency_ms:.0f}ms"
                    )

        manager.subscribe("BTC/USDT", on_update)
        time.sleep(duration)

        health = manager.get_health("BTC/USDT")
        manager.stop()

        print(f"\n  Results:")
        print(f"     Updates: {stats['count']} ({stats['count']/duration:.1f}/s)")
        print(f"     Healthy: {'✅' if health and health.is_healthy else '❌'}")

        if stats['spreads']:
            avg_spread = sum(stats['spreads']) / len(stats['spreads'])
            print(f"     Avg Spread: {avg_spread:.2f} bps")
        if stats['latencies']:
            avg_latency = sum(stats['latencies']) / len(stats['latencies'])
            print(f"     Avg Latency: {avg_latency:.1f} ms")

        return stats["count"] > duration

    @staticmethod
    def multi_symbol(duration: int = 30, num_pairs: int = 10) -> bool:
        """Test multi-symbol streaming."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed - skipping")
            return True

        symbols = ALL_TRADING_SYMBOLS[:num_pairs]
        print(f"\n  Testing {len(symbols)} symbols for {duration}s...")

        manager = OrderBookManager(multi_symbol=True, store_history=False)
        counts: Dict[str, int] = {s: 0 for s in symbols}

        def on_update(snap: OrderBookSnapshot) -> None:
            counts[snap.symbol] = counts.get(snap.symbol, 0) + 1

        manager.subscribe_many(symbols, on_update)

        def report(elapsed: float) -> None:
            total = sum(counts.values())
            active = sum(1 for c in counts.values() if c > 0)
            rate = total / max(1, elapsed)
            print(
                f"     [{elapsed:5.0f}s] "
                f"Updates: {total:6d} ({rate:6.1f}/s) | "
                f"Active: {active}/{len(symbols)}"
            )

        timed_loop(duration, 5, report)

        pool_health = manager.get_pool_health()
        manager.stop()

        total = sum(counts.values())
        active = sum(1 for c in counts.values() if c > 0)

        print(f"\n  Results:")
        print(f"     Total: {total} updates ({total/duration:.1f}/s)")
        print(f"     Active: {active}/{len(symbols)}")
        print(f"     Status: {pool_health.get('status', 'unknown')}")

        top_5 = sorted(counts.items(), key=lambda x: -x[1])[:5]
        print(f"     Top 5: {', '.join(f'{s}:{c}' for s, c in top_5)}")

        return active >= len(symbols) * 0.8

    @staticmethod
    def stress(duration: int = 60) -> bool:
        """Full stress test with maximum symbols."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed - skipping")
            return True

        manager = OrderBookManager(multi_symbol=True, store_history=False)
        max_symbols = min(len(ALL_TRADING_SYMBOLS), manager._stream.max_capacity)
        symbols = ALL_TRADING_SYMBOLS[:max_symbols]

        print(f"\n  Stress testing {len(symbols)} symbols for {duration}s...")
        print(f"  Capacity: {manager._stream.max_capacity}")

        counts: Dict[str, int] = {s: 0 for s in symbols}

        def on_update(snap: OrderBookSnapshot) -> None:
            counts[snap.symbol] = counts.get(snap.symbol, 0) + 1

        manager.subscribe_many(symbols, on_update)

        def report(elapsed: float) -> None:
            total = sum(counts.values())
            active = sum(1 for c in counts.values() if c > 0)
            rate = total / max(1, elapsed)

            health = manager.get_all_health()
            unhealthy = [s for s, h in health.items() if not h.is_healthy]

            print(
                f"   [{elapsed:6.0f}s] "
                f"Updates: {total:7d} ({rate:6.1f}/s) | "
                f"Active: {active}/{len(symbols)} | "
                f"Unhealthy: {len(unhealthy)}"
            )

        timed_loop(duration, 10, report)

        pool_health = manager.get_pool_health()
        manager.stop()

        total = sum(counts.values())
        active = sum(1 for c in counts.values() if c > 0)
        dead = [s for s, c in counts.items() if c == 0]

        print(f"\n  Results:")
        print(f"     Total: {total:,} updates ({total/duration:.1f}/s)")
        print(f"     Active: {active}/{len(symbols)} ({100*active/len(symbols):.1f}%)")
        print(f"     Status: {pool_health.get('status', 'unknown')}")

        if dead:
            print(f"\n  ⚠️ Dead symbols ({len(dead)}): {dead[:10]}")

        return active >= len(symbols) * 0.9

    @staticmethod
    def reconnect(duration: int = 120) -> bool:
        """Test reconnection stability."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed - skipping")
            return True

        print(f"\n  Testing reconnection stability for {duration}s...")

        manager = OrderBookManager(multi_symbol=True, store_history=False)
        symbols = ALL_TRADING_SYMBOLS[:20]
        counts: Dict[str, int] = {s: 0 for s in symbols}

        def on_update(snap: OrderBookSnapshot) -> None:
            counts[snap.symbol] = counts.get(snap.symbol, 0) + 1

        manager.subscribe_many(symbols, on_update)

        def report(elapsed: float) -> None:
            total = sum(counts.values())
            health = manager.get_all_health()

            reconnects = sum(h.reconnect_count for h in health.values())
            errors = sum(h.error_count for h in health.values())

            print(
                f"   [{elapsed:5.0f}s] "
                f"Updates: {total:6d} | "
                f"Reconnects: {reconnects} | "
                f"Errors: {errors}"
            )

        timed_loop(duration, 15, report)

        pool_health = manager.get_pool_health()
        manager.stop()

        reconnects = pool_health.get('total_reconnects', 0)
        errors = pool_health.get('total_errors', 0)

        print(f"\n  Results:")
        print(f"     Total Reconnects: {reconnects}")
        print(f"     Total Errors: {errors}")
        print(f"     Status: {pool_health.get('status', 'unknown')}")

        return pool_health.get('status') != 'down'


def run_live_tests(suite: TestSuite, test_type: str = "all", duration: int = 30) -> bool:
    """Run live tests."""
    if not HAS_WEBSOCKETS:
        print("\n  ⚠️ websockets not installed - skipping live tests")
        return True

    print_header(f"LIVE TESTS ({duration}s)")

    if test_type in ("all", "single", "live"):
        suite.run_test("Single Symbol", LiveTests.single_symbol, min(duration, 30))

    if test_type in ("all", "multi"):
        suite.run_test("Multi Symbol", LiveTests.multi_symbol, min(duration, 30), 10)

    if test_type in ("all", "stress"):
        suite.run_test("Stress Test", LiveTests.stress, duration)

    if test_type in ("reconnect",):
        suite.run_test("Reconnect Test", LiveTests.reconnect, duration)

    return True


# ============================================================
# DIAGNOSTICS
# ============================================================

class Diagnostics:
    """Diagnostic tools for troubleshooting."""

    @staticmethod
    def diagnose_unhealthy(duration: int = 120) -> Dict[str, Any]:
        """Diagnose why symbols become unhealthy."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed")
            return {}

        print_header(f"DIAGNOSING UNHEALTHY SYMBOLS ({duration}s)")

        manager = OrderBookManager(multi_symbol=True, store_history=False)
        symbols = ALL_TRADING_SYMBOLS[:80]

        tracking = {
            s: {
                "count": 0,
                "first_update": None,
                "last_update": None,
                "gaps": [],
            }
            for s in symbols
        }

        def on_update(snap: OrderBookSnapshot) -> None:
            now = time.time()
            t = tracking[snap.symbol]

            if t["first_update"] is None:
                t["first_update"] = now

            if t["last_update"] is not None:
                gap = now - t["last_update"]
                if gap > 5.0:
                    t["gaps"].append((t["last_update"], now, gap))

            t["last_update"] = now
            t["count"] += 1

        print(f"\n  Monitoring {len(symbols)} symbols...")
        manager.subscribe_many(symbols, on_update)

        start = time.time()
        check_interval = 15
        last_check = 0

        while time.time() - start < duration:
            elapsed = time.time() - start

            if elapsed - last_check >= check_interval:
                last_check = elapsed

                health = manager.get_all_health()
                unhealthy = [s for s, h in health.items() if not h.is_healthy]

                now = time.time()
                stalled = []
                for s, t in tracking.items():
                    if t["last_update"] and now - t["last_update"] > 10:
                        stalled.append(s)

                print(
                    f"   [{elapsed:5.0f}s] "
                    f"Unhealthy: {len(unhealthy)} | "
                    f"Stalled: {len(stalled)}"
                )

                if unhealthy[:3]:
                    for s in unhealthy[:3]:
                        h = health.get(s)
                        if h:
                            print(
                                f"      {s}: age={h.age_ms}ms, "
                                f"lat={h.latency_ms:.0f}ms, "
                                f"err={h.error_count}"
                            )

            time.sleep(1)

        final_health = manager.get_all_health()
        manager.stop()

        results = {
            "healthy": [],
            "unhealthy": [],
            "never_updated": [],
            "had_gaps": [],
        }

        for s, t in tracking.items():
            h = final_health.get(s)

            if t["count"] == 0:
                results["never_updated"].append(s)
            elif t["gaps"]:
                results["had_gaps"].append((s, len(t["gaps"]), t["gaps"][-1][2]))
            elif h and not h.is_healthy:
                results["unhealthy"].append(s)
            else:
                results["healthy"].append(s)

        print("\n" + "-" * 70)
        print(" DIAGNOSIS RESULTS")
        print("-" * 70)

        print(f"\n  Healthy: {len(results['healthy'])}")
        print(f"  Unhealthy: {len(results['unhealthy'])}")
        print(f"  Never updated: {len(results['never_updated'])}")
        print(f"  Had gaps: {len(results['had_gaps'])}")

        if results["unhealthy"]:
            print(f"\n  Unhealthy symbols: {results['unhealthy'][:20]}")

        if results["never_updated"]:
            print(f"\n  Never updated: {results['never_updated'][:20]}")

        if results["had_gaps"]:
            print(f"\n  Symbols with gaps (top 10):")
            for s, gap_count, last_gap in sorted(
                results["had_gaps"], key=lambda x: -x[1]
            )[:10]:
                print(f"     {s}: {gap_count} gaps, last={last_gap:.1f}s")

        return results

    @staticmethod
    def connection_stats() -> None:
        """Display connection configuration."""
        print_header("CONNECTION CONFIGURATION")

        print("\n  WebSocket Settings:")
        print(f"     Ping interval: {OrderBookConstants.WS_PING_INTERVAL}s")
        print(f"     Ping timeout:  {OrderBookConstants.WS_PING_TIMEOUT}s")
        print(f"     Recv timeout:  {OrderBookConstants.WS_RECV_TIMEOUT}s")
        print(f"     Max message:   {OrderBookConstants.WS_MAX_MESSAGE_SIZE / 1024 / 1024:.0f}MB")

        print("\n  Reconnection:")
        print(f"     Max attempts: {OrderBookConstants.MAX_RECONNECT_ATTEMPTS}")
        print(f"     Backoff:      {OrderBookConstants.INITIAL_BACKOFF}s - {OrderBookConstants.MAX_BACKOFF}s")

        print("\n  Multi-Stream Capacity:")
        print(
            f"     {BinanceMultiStream.MAX_STREAMS_PER_CONNECTION}/conn × "
            f"{BinanceMultiStream.MAX_CONNECTIONS} = "
            f"{BinanceMultiStream.MAX_STREAMS_PER_CONNECTION * BinanceMultiStream.MAX_CONNECTIONS} max"
        )

        print("\n  Health Thresholds:")
        print(f"     Stale data:   {OrderBookConstants.STALE_DATA_MS}ms")
        print(f"     Spread alert: {OrderBookConstants.MAX_SPREAD_BPS_ALERT}bps")
        print(f"     Price spike:  {OrderBookConstants.MAX_PRICE_SPIKE_PCT}%")

        print("\n  Execution Defaults:")
        print(f"     Fee:      {OrderBookConstants.DEFAULT_FEE_BPS} bps")
        print(f"     Slippage: {OrderBookConstants.DEFAULT_SLIPPAGE_BPS} bps")
        print(f"     Large trade: {OrderBookConstants.LARGE_TRADE_THRESHOLD}")

        print("\n  History Settings:")
        print(f"     Flush size:    {OrderBookConstants.HISTORY_FLUSH_SIZE}")
        print(f"     Max snapshots: {OrderBookConstants.MAX_SNAPSHOTS_DEFAULT}")
        print(f"     Flush threads: {OrderBookConstants.MAX_FLUSH_THREADS}")

    @staticmethod
    def test_specific_symbols(symbols: List[str], duration: int = 30) -> Dict[str, Any]:
        """Test specific symbols for debugging."""
        if not HAS_WEBSOCKETS:
            print("   ⚠️ websockets not installed")
            return {}

        print(f"\n  Testing specific symbols: {symbols}")
        print(f"  Duration: {duration}s")

        manager = OrderBookManager(multi_symbol=True, store_history=False)
        counts = {s: 0 for s in symbols}
        first_update = {s: None for s in symbols}
        last_update = {s: None for s in symbols}

        def on_update(snap):
            now = time.time()
            counts[snap.symbol] = counts.get(snap.symbol, 0) + 1
            if first_update[snap.symbol] is None:
                first_update[snap.symbol] = now
            last_update[snap.symbol] = now

        manager.subscribe_many(symbols, on_update)

        def report(elapsed: float) -> None:
            total = sum(counts.values())
            active = sum(1 for c in counts.values() if c > 0)
            print(f"     [{elapsed:5.0f}s] Updates: {total} | Active: {active}/{len(symbols)}")

        timed_loop(duration, 5, report)

        results = {}
        for s in symbols:
            h = manager.get_health(s)
            results[s] = {
                "count": counts.get(s, 0),
                "rate": counts.get(s, 0) / duration,
                "healthy": h.is_healthy if h else False,
                "latency": h.latency_ms if h else 0,
                "errors": h.error_count if h else 0,
                "reconnects": h.reconnect_count if h else 0,
            }

        manager.stop()

        print("\n  Results:")
        for s, r in results.items():
            status = "✅" if r["healthy"] else "❌"
            print(
                f"     {status} {s}: {r['count']} updates ({r['rate']:.1f}/s), "
                f"lat={r['latency']:.0f}ms, err={r['errors']}"
            )

        return results

    @staticmethod
    def capacity_test() -> None:
        """Test symbol capacity limits."""
        print_header("CAPACITY LIMITS TEST")

        stream = BinanceMultiStream()
        max_cap = stream.max_capacity

        print(f"\n  Configuration:")
        print(f"     Max streams per connection: {stream.MAX_STREAMS_PER_CONNECTION}")
        print(f"     Max connections: {stream.MAX_CONNECTIONS}")
        print(f"     Total capacity: {max_cap}")

        print(f"\n  Chunk distribution:")
        for size in [10, 50, 100, 200, 300, 400, 500]:
            symbols = [f"S{i}/USDT" for i in range(size)]
            chunks = stream._chunk_symbols(symbols)
            actual = sum(len(c) for c in chunks)
            capped = min(size, max_cap)

            if actual >= capped:
                status = "✅"
            else:
                status = f"⚠️ truncated to {actual}"

            print(f"     {size:4d} symbols → {len(chunks)} chunks {status}")

    @staticmethod
    def url_length_test() -> None:
        """Test URL length limits."""
        print_header("URL LENGTH TEST")

        stream = BinanceMultiStream()

        print(f"\n  Max URL length: {OrderBookConstants.MAX_URL_LENGTH}")

        for count in [10, 50, 100, 150, 200]:
            symbols = ALL_TRADING_SYMBOLS[:count]
            url = stream._build_stream_url(symbols)

            status = "✅" if len(url) <= OrderBookConstants.MAX_URL_LENGTH else "❌ EXCEEDS"
            print(f"     {count} symbols: {len(url)} chars {status}")


# ============================================================
# INTEGRATION TESTS
# ============================================================

class IntegrationTests:
    """Full workflow integration tests."""

    @staticmethod
    def backtest_workflow() -> bool:
        """Test complete backtest workflow."""
        random.seed(42)

        manager = OrderBookManager(backtest_mode=True)

        candles = [
            {
                "timestamp": i * 60000,
                "open": 50000 + random.uniform(-500, 500),
                "high": 50000 + random.uniform(0, 600),
                "low": 50000 - random.uniform(0, 600),
                "close": 50000 + random.uniform(-300, 300),
                "volume": random.uniform(100, 1000),
            }
            for i in range(100)
        ]

        manager.load_backtest_candles("BTC/USDT", candles)

        analyzer = manager.get_analyzer("BTC/USDT")
        if not analyzer or analyzer.count != 100:
            print(f"   Expected 100 snapshots, got {analyzer.count if analyzer else 0}")
            return False

        snapshot = manager.get_snapshot("BTC/USDT")
        if not snapshot or not snapshot.is_valid:
            print("   Invalid snapshot")
            return False

        snap_at_time = manager.get_backtest_snapshot("BTC/USDT", 50 * 60000)
        if not snap_at_time:
            print("   Time-based lookup failed")
            return False

        signals = []
        for i in range(10, 100, 5):
            ts = i * 60000
            snap = manager.get_backtest_snapshot("BTC/USDT", ts)
            if snap and snap.is_valid:
                signals.append({
                    "time": ts,
                    "mid": snap.mid_price,
                    "imbalance": snap.imbalance,
                    "signal": analyzer.get_signal_strength(),
                })

        manager.stop()

        print(f"   Generated {len(signals)} signals")
        print(f"   Mid price range: ${min(s['mid'] for s in signals):,.2f} - ${max(s['mid'] for s in signals):,.2f}")

        return len(signals) > 0

    @staticmethod
    def execution_simulation() -> bool:
        """Test execution simulation integration."""
        random.seed(42)

        manager = OrderBookManager(backtest_mode=True)

        candles = [
            {
                "timestamp": i * 60000,
                "open": 50000 + i * 10,
                "high": 50000 + i * 10 + 50,
                "low": 50000 + i * 10 - 50,
                "close": 50000 + i * 10 + 20,
                "volume": random.uniform(100, 1000),
            }
            for i in range(50)
        ]
        manager.load_backtest_candles("BTC/USDT", candles)

        simulator = BacktestExecutionSimulator(
            slippage_model="realistic",
            fee_bps=4.0
        )

        results = []
        for i in range(10, 50, 5):
            ob = manager.get_backtest_snapshot("BTC/USDT", i * 60000)
            if ob and ob.is_valid:
                entry_price = ob.mid_price
                exit_price = entry_price * 1.002

                result = simulator.simulate_round_trip(
                    entry_price,
                    exit_price,
                    0.01,
                    "buy",
                    entry_orderbook=ob
                )
                results.append(result)

        manager.stop()

        total_pnl = sum(r["net_pnl"] for r in results)
        wins = sum(1 for r in results if r["profitable"])

        print(f"   Trades: {len(results)}")
        print(f"   Wins: {wins}/{len(results)} ({100*wins/len(results):.1f}%)")
        print(f"   Total PnL: ${total_pnl:.4f}")

        stats = simulator.get_statistics()
        print(f"   Total Fees: ${stats['total_fees']:.6f}")
        print(f"   Total Slippage: ${stats['total_slippage_cost']:.6f}")

        return len(results) > 0

    @staticmethod
    def order_flow_integration() -> bool:
        """Test order flow tracking integration."""
        tracker = OrderFlowTracker(large_trade_threshold=1.0)

        trades = []
        base_price = 50000.0
        for i in range(100):
            if i % 3 == 0:
                price = base_price + random.uniform(0.1, 1.0)
            elif i % 3 == 1:
                price = base_price - random.uniform(0.1, 1.0)
            else:
                price = base_price

            size = random.uniform(0.1, 2.0)
            trades.append((price, size))
            base_price = price

        metrics = tracker.process_batch(trades)

        print(f"   Trades processed: {metrics.total_trade_count}")
        print(f"   Buy volume: {metrics.buy_volume:.2f}")
        print(f"   Sell volume: {metrics.sell_volume:.2f}")
        print(f"   Cumulative delta: {metrics.cumulative_delta:+.2f}")
        print(f"   OFI: {metrics.ofi:+.3f}")
        print(f"   Large buys: {metrics.large_buy_count}")
        print(f"   Large sells: {metrics.large_sell_count}")
        print(f"   Classification rate: {metrics.classification_rate*100:.1f}%")

        return metrics.total_trade_count == 100

    @staticmethod
    def synthetic_orderbook_integration() -> bool:
        """Test synthetic order book generation integration."""
        random.seed(42)

        ticks = []
        price = 50000.0
        for i in range(500):
            price += random.uniform(-10, 10)
            ticks.append((i * 100, price))

        tick_sizes = [random.uniform(0.1, 2.0) for _ in range(500)]

        analyzer = SyntheticOrderBook.generate_analyzer(
            ticks,
            tick_sizes=tick_sizes,
            sample_every=10,
            spread_bps=5.0,
            levels=20
        )

        if analyzer is None:
            print("   Failed to generate analyzer")
            return False

        print(f"   Generated {analyzer.count} snapshots")

        stats = analyzer.get_statistics()
        print(f"   Avg spread: {stats['avg_spread_pct']*100:.4f}%")
        print(f"   Signal strength: {stats['signal_strength']:+.3f}")

        current = analyzer.current
        if current and current.is_valid:
            print(f"   Current mid: ${current.mid_price:,.2f}")
            print(f"   Current spread: {current.spread_bps:.2f} bps")

        return analyzer.count > 0

    @staticmethod
    def data_validation_integration() -> bool:
        """Test data validation integration."""
        validator = DataValidator(
            max_spread_bps=100.0,
            max_price_spike_pct=5.0,
            max_symbols=100
        )

        valid_count = 0
        warning_count = 0
        invalid_count = 0

        for i in range(100):
            if i % 20 == 0:
                snap = OrderBookSnapshot.from_raw(
                    f"SYM{i}/USDT",
                    [(102, 10)],
                    [(101, 10)]
                )
            elif i % 10 == 0:
                snap = OrderBookSnapshot.from_raw(
                    f"SYM{i}/USDT",
                    [(100, 10)],
                    [(200, 10)]
                )
            else:
                snap = OrderBookSnapshot.from_raw(
                    f"SYM{i}/USDT",
                    [(100, 10)],
                    [(100.1, 10)]
                )

            is_valid, warnings = validator.validate(snap)

            if not is_valid:
                invalid_count += 1
            elif warnings:
                warning_count += 1
            else:
                valid_count += 1

        print(f"   Valid: {valid_count}")
        print(f"   Warnings: {warning_count}")
        print(f"   Invalid: {invalid_count}")

        stats = validator.get_stats()
        print(f"   Tracked symbols: {stats['tracked_symbols']}")

        return valid_count > 0 and invalid_count > 0

    @staticmethod
    def manager_backtest_mode() -> bool:
        """Test OrderBookManager in backtest mode."""
        manager = OrderBookManager(backtest_mode=True)

        if not manager.backtest_mode:
            print("   Manager not in backtest mode")
            return False

        ticks = [(i * 1000, 50000 + i * 0.1) for i in range(100)]
        manager.load_backtest_ticks("BTC/USDT", ticks, sample_every=10)

        analyzer = manager.get_analyzer("BTC/USDT")
        if analyzer is None:
            print("   No analyzer found")
            return False

        manager.set_backtest_time(50 * 1000)

        snap = manager.get_backtest_snapshot("BTC/USDT", 50 * 1000)
        if snap is None:
            print("   No snapshot at time")
            return False

        pool_health = manager.get_pool_health()
        if pool_health.get("status") != "backtest_mode":
            print(f"   Unexpected status: {pool_health.get('status')}")
            return False

        manager.stop()

        print(f"   Snapshots: {analyzer.count}")
        print(f"   Pool status: {pool_health.get('status')}")

        return True

    @staticmethod
    def full_trading_simulation() -> bool:
        """Simulate a complete trading workflow."""
        random.seed(42)

        manager = OrderBookManager(backtest_mode=True)

        candles = []
        price = 50000.0
        for i in range(200):
            change = random.uniform(-100, 100)
            open_p = price
            close_p = price + change
            high_p = max(open_p, close_p) + random.uniform(0, 50)
            low_p = min(open_p, close_p) - random.uniform(0, 50)

            candles.append({
                "timestamp": i * 60000,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": random.uniform(100, 1000),
            })
            price = close_p

        manager.load_backtest_candles("BTC/USDT", candles)

        simulator = BacktestExecutionSimulator(
            slippage_model="realistic",
            fee_bps=4.0
        )
        tracker = OrderFlowTracker(large_trade_threshold=1.0)

        trades = []
        position = 0.0
        entry = None

        for i in range(50, 200):
            ob = manager.get_backtest_snapshot("BTC/USDT", i * 60000)
            if not ob or not ob.is_valid:
                continue

            analyzer = manager.get_analyzer("BTC/USDT")
            signal = analyzer.get_signal_strength() if analyzer else 0.0

            for _ in range(random.randint(5, 20)):
                trade_price = ob.mid_price + random.uniform(-10, 10)
                trade_size = random.uniform(0.01, 0.5)
                tracker.process_trade(trade_price, trade_size)

            if position == 0 and signal > 0.3:
                entry = simulator.simulate_entry(
                    ob.mid_price, 0.1, "buy", orderbook=ob
                )
                position = 0.1
            elif position > 0 and (signal < -0.2 or (entry and ob.mid_price > entry["fill_price"] * 1.005)):
                if entry:
                    result = simulator.simulate_exit(entry, ob.mid_price, orderbook=ob)
                    trades.append(result)
                    entry = None
                position = 0.0

        manager.stop()

        if trades:
            total_pnl = sum(t["net_pnl"] for t in trades)
            wins = sum(1 for t in trades if t["profitable"])
            avg_pnl = total_pnl / len(trades)

            print(f"   Total trades: {len(trades)}")
            print(f"   Win rate: {100*wins/len(trades):.1f}%")
            print(f"   Total PnL: ${total_pnl:.2f}")
            print(f"   Avg PnL/trade: ${avg_pnl:.4f}")
        else:
            print("   No trades executed")

        flow_metrics = tracker.get_metrics()
        print(f"   Order flow OFI: {flow_metrics.ofi:+.3f}")

        return True


def run_integration_tests(suite: TestSuite) -> bool:
    """Run integration tests."""
    print_header("INTEGRATION TESTS")

    tests = [
        ("Backtest Workflow", IntegrationTests.backtest_workflow),
        ("Execution Simulation", IntegrationTests.execution_simulation),
        ("Order Flow Integration", IntegrationTests.order_flow_integration),
        ("Synthetic OrderBook", IntegrationTests.synthetic_orderbook_integration),
        ("Data Validation", IntegrationTests.data_validation_integration),
        ("Manager Backtest Mode", IntegrationTests.manager_backtest_mode),
        ("Full Trading Simulation", IntegrationTests.full_trading_simulation),
    ]

    all_passed = True
    for name, test_fn in tests:
        if not suite.run_test(name, test_fn):
            all_passed = False

    return all_passed


# ============================================================
# MAIN CLI
# ============================================================

def print_banner() -> None:
    """Print the test suite banner."""
    print("\n" + "=" * 70)
    print(" ORACLE ORDER BOOK v5.0 - THE BENCHMARK SUITE FROM HELL")
    print("=" * 70)
    print(f" Python:     {sys.version.split()[0]}")
    print(f" WebSockets: {'✅ Available' if HAS_WEBSOCKETS else '❌ Not installed'}")
    print(f" CCXT Pro:   {'✅ Available' if HAS_CCXT_PRO else '❌ Not installed'}")
    print(f" Time:       {datetime.now().isoformat()}")
    print("=" * 70)


def print_usage() -> None:
    """Print usage information."""
    print("""
Usage: python oracle_orderbook_test.py [command] [args]

Commands:
  unit                  Run unit tests (no network required)
  bench [type]          Run benchmarks (parsing|lookup|recent|pipeline|metrics|analyzer|memory)
  live [sec]            Live single-symbol test (default 30s)
  multi [sec] [n]       Multi-symbol test (default 30s, 10 pairs)
  stress [sec]          Stress test all symbols (default 60s)
  reconnect [sec]       Reconnection stability test (default 120s)
  diagnose [sec]        Diagnose unhealthy symbols (default 120s)
  stats                 Show connection configuration
  limits                Show capacity limits
  memory                Run memory benchmarks
  backtest              Run backtest simulation test
  execution             Run execution simulator test
  integration           Run all integration tests
  all                   Run everything
  quick                 Quick smoke test (unit tests only)
  help                  Show this help

Examples:
  python oracle_orderbook_test.py unit
  python oracle_orderbook_test.py bench parsing
  python oracle_orderbook_test.py live 60
  python oracle_orderbook_test.py multi 30 20
  python oracle_orderbook_test.py stress 86400
  python oracle_orderbook_test.py diagnose 300
  python oracle_orderbook_test.py all

Environment Variables:
  DEBUG=1               Enable debug output with full tracebacks
    """)


def main() -> None:
    """Main entry point."""
    print_banner()

    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1].lower()
    args = sys.argv[2:]

    suite = TestSuite("Oracle OrderBook")

    if command == "help" or command == "-h" or command == "--help":
        print_usage()
        return

    elif command == "unit":
        run_unit_tests(suite)
        suite.print_summary()

    elif command == "bench":
        specific = args[0] if args else None
        run_benchmarks(suite, specific)

    elif command == "live":
        duration = int(args[0]) if args else 30
        suite.run_test(f"Live Stream ({duration}s)", LiveTests.single_symbol, duration)
        suite.print_summary()

    elif command == "multi":
        duration = int(args[0]) if args else 30
        num_pairs = int(args[1]) if len(args) > 1 else 10
        suite.run_test(
            f"Multi Symbol ({num_pairs} pairs, {duration}s)",
            LiveTests.multi_symbol,
            duration,
            num_pairs
        )
        suite.print_summary()

    elif command == "stress":
        duration = int(args[0]) if args else 60
        suite.run_test(f"Stress Test ({duration}s)", LiveTests.stress, duration)
        suite.print_summary()

    elif command == "reconnect":
        duration = int(args[0]) if args else 120
        suite.run_test(f"Reconnection Test ({duration}s)", LiveTests.reconnect, duration)
        suite.print_summary()

    elif command == "diagnose":
        duration = int(args[0]) if args else 120
        Diagnostics.diagnose_unhealthy(duration)

    elif command == "stats":
        Diagnostics.connection_stats()

    elif command == "limits":
        Diagnostics.capacity_test()
        Diagnostics.url_length_test()

    elif command == "memory":
        Benchmarks.memory(suite)

    elif command == "backtest":
        suite.run_test("Backtest Workflow", IntegrationTests.backtest_workflow)
        suite.print_summary()

    elif command == "execution":
        suite.run_test("Execution Simulation", IntegrationTests.execution_simulation)
        suite.print_summary()

    elif command == "integration":
        run_integration_tests(suite)
        suite.print_summary()

    elif command == "quick":
        run_unit_tests(suite)
        suite.print_summary()

    elif command == "all":
        print("\n" + "=" * 70)
        print(" RUNNING COMPLETE TEST SUITE")
        print("=" * 70)

        run_unit_tests(suite)

        run_benchmarks(suite)

        run_integration_tests(suite)

        if HAS_WEBSOCKETS:
            run_live_tests(suite, "all", 15)
        else:
            print("\n  ⚠️ Skipping live tests - websockets not installed")

        suite.print_summary()

        summary = suite.summary()
        if summary["failed"] == 0:
            print("\n" + "=" * 70)
            print(" 🏆 THE ORACLE IS ALIVE AND WELL")
            print("=" * 70)
        else:
            print("\n" + "=" * 70)
            print(f" ⚠️ {summary['failed']} TESTS NEED ATTENTION")
            print("=" * 70)

    else:
        print(f"\n  ❌ Unknown command: {command}")
        print("     Run 'python oracle_orderbook_test.py help' for usage")
        sys.exit(1)

    summary = suite.summary()
    sys.exit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
