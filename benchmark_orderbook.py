#!/usr/bin/env python3
"""
Benchmark script for oracle_orderbook.py optimizations.

Usage:
    python benchmark_orderbook.py           # Run all benchmarks
    python benchmark_orderbook.py parsing   # Test parsing speed
    python benchmark_orderbook.py memory    # Test memory usage
    python benchmark_orderbook.py lookup    # Test symbol lookup
    python benchmark_orderbook.py live      # Test live throughput
"""

import gc
import sys
import time
import random
import statistics
from typing import List, Dict, Any, Callable, Tuple
from collections import deque

# Import your orderbook module
from oracle_orderbook import (
    OrderBookSnapshot,
    OrderBookLevel,
    OrderBookAnalyzer,
    BinanceMultiStream,
    _now_ms,
)


# ============================================================
# BENCHMARK UTILITIES
# ============================================================

def benchmark(func: Callable, iterations: int = 1000, warmup: int = 100) -> Dict[str, float]:
    """
    Run a function multiple times and collect timing statistics.
    
    Returns dict with: mean, median, min, max, std, ops_per_sec
    """
    # Warmup
    for _ in range(warmup):
        func()
    
    # Force garbage collection before timing
    gc.collect()
    gc.disable()
    
    times = []
    try:
        for _ in range(iterations):
            start = time.perf_counter_ns()
            func()
            end = time.perf_counter_ns()
            times.append((end - start) / 1_000_000)  # Convert to ms
    finally:
        gc.enable()
    
    mean_time = statistics.mean(times)
    return {
        "mean_ms": mean_time,
        "median_ms": statistics.median(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "std_ms": statistics.stdev(times) if len(times) > 1 else 0,
        "ops_per_sec": 1000 / mean_time if mean_time > 0 else float('inf'),
        "iterations": iterations,
    }


def print_benchmark_result(name: str, result: Dict[str, float]) -> None:
    """Pretty print benchmark results."""
    print(f"\n  {name}:")
    print(f"    Mean:     {result['mean_ms']:.4f} ms")
    print(f"    Median:   {result['median_ms']:.4f} ms")
    print(f"    Min/Max:  {result['min_ms']:.4f} / {result['max_ms']:.4f} ms")
    print(f"    Std Dev:  {result['std_ms']:.4f} ms")
    print(f"    Throughput: {result['ops_per_sec']:,.0f} ops/sec")


def compare_results(name1: str, result1: Dict, name2: str, result2: Dict) -> None:
    """Compare two benchmark results."""
    speedup = result1['mean_ms'] / result2['mean_ms'] if result2['mean_ms'] > 0 else float('inf')
    print(f"\n  📊 Comparison:")
    print(f"    {name1}: {result1['mean_ms']:.4f} ms ({result1['ops_per_sec']:,.0f} ops/sec)")
    print(f"    {name2}: {result2['mean_ms']:.4f} ms ({result2['ops_per_sec']:,.0f} ops/sec)")
    print(f"    Speedup: {speedup:.2f}x {'faster' if speedup > 1 else 'slower'}")


def generate_binance_data(levels: int = 20) -> Tuple[List[List[str]], List[List[str]]]:
    """Generate realistic Binance-style order book data."""
    base_price = 50000.0 + random.uniform(-1000, 1000)
    spread = base_price * 0.0001  # 1 bps spread
    
    bids = []
    asks = []
    
    for i in range(levels):
        bid_price = base_price - spread/2 - i * spread * 0.5
        ask_price = base_price + spread/2 + i * spread * 0.5
        size = random.uniform(0.1, 10.0)
        
        # Binance sends strings
        bids.append([f"{bid_price:.2f}", f"{size:.8f}"])
        asks.append([f"{ask_price:.2f}", f"{size:.8f}"])
    
    return bids, asks


# ============================================================
# BENCHMARK 1: PARSING SPEED
# ============================================================

def benchmark_parsing():
    """Compare from_raw vs from_binance_fast parsing speed."""
    print("\n" + "=" * 70)
    print(" BENCHMARK 1: PARSING SPEED")
    print("=" * 70)
    
    # Generate test data
    bids, asks = generate_binance_data(20)
    timestamp = _now_ms()
    symbol = "BTC/USDT"
    
    # Also create tuple version for from_raw
    bids_tuples = [(float(p), float(s)) for p, s in bids]
    asks_tuples = [(float(p), float(s)) for p, s in asks]
    
    # Benchmark from_raw (original slow path)
    def test_from_raw():
        return OrderBookSnapshot.from_raw(
            symbol=symbol,
            bids=bids_tuples,
            asks=asks_tuples,
            timestamp=timestamp,
            exchange="binance"
        )
    
    # Benchmark from_binance_fast (optimized path)
    def test_from_binance_fast():
        return OrderBookSnapshot.from_binance_fast(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            sequence=12345
        )
    
    print("\n  Testing with 20 levels, 10,000 iterations...")
    
    result_raw = benchmark(test_from_raw, iterations=10000)
    result_fast = benchmark(test_from_binance_fast, iterations=10000)
    
    print_benchmark_result("from_raw (original)", result_raw)
    print_benchmark_result("from_binance_fast (optimized)", result_fast)
    compare_results("from_raw", result_raw, "from_binance_fast", result_fast)
    
    # Verify correctness
    snap_raw = test_from_raw()
    snap_fast = test_from_binance_fast()
    
    print("\n  ✅ Correctness check:")
    print(f"    from_raw mid_price:        {snap_raw.mid_price:.2f}")
    print(f"    from_binance_fast mid_price: {snap_fast.mid_price:.2f}")
    print(f"    Match: {'✅ YES' if abs(snap_raw.mid_price - snap_fast.mid_price) < 0.01 else '❌ NO'}")
    
    return result_raw, result_fast


# ============================================================
# BENCHMARK 2: MEMORY USAGE
# ============================================================

def benchmark_memory():
    """Compare memory usage with and without __slots__."""
    print("\n" + "=" * 70)
    print(" BENCHMARK 2: MEMORY USAGE")
    print("=" * 70)
    
    import sys
    
    # Create single instances
    bids, asks = generate_binance_data(20)
    timestamp = _now_ms()
    
    snap = OrderBookSnapshot.from_binance_fast("BTC/USDT", bids, asks, timestamp)
    level = snap.bids[0] if snap.bids else OrderBookLevel(100.0, 1.0)
    
    # Check if __slots__ is being used
    has_slots_snapshot = hasattr(OrderBookSnapshot, '__slots__')
    has_slots_level = hasattr(OrderBookLevel, '__slots__')
    
    print(f"\n  __slots__ enabled:")
    print(f"    OrderBookSnapshot: {'✅ YES' if has_slots_snapshot else '❌ NO'}")
    print(f"    OrderBookLevel:    {'✅ YES' if has_slots_level else '❌ NO'}")
    
    # Measure object sizes
    level_size = sys.getsizeof(level)
    snap_size = sys.getsizeof(snap)
    
    # Measure with many objects
    gc.collect()
    
    import tracemalloc
    tracemalloc.start()
    
    snapshots = []
    for i in range(1000):
        bids, asks = generate_binance_data(20)
        snapshots.append(
            OrderBookSnapshot.from_binance_fast(f"SYM{i}/USDT", bids, asks, timestamp)
        )
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"\n  Memory per object:")
    print(f"    OrderBookLevel: {level_size} bytes")
    print(f"    OrderBookSnapshot: {snap_size} bytes (excluding lists)")
    
    print(f"\n  Memory for 1,000 snapshots (20 levels each):")
    print(f"    Current: {current / 1024 / 1024:.2f} MB")
    print(f"    Peak:    {peak / 1024 / 1024:.2f} MB")
    print(f"    Per snapshot: {current / 1000 / 1024:.2f} KB")
    
    # Expected values for comparison
    print(f"\n  📊 Reference (typical values):")
    print(f"    Without __slots__: ~1.5-2.0 KB per snapshot")
    print(f"    With __slots__:    ~0.5-0.8 KB per snapshot")
    
    return current, peak


# ============================================================
# BENCHMARK 3: SYMBOL LOOKUP SPEED
# ============================================================

def benchmark_lookup():
    """Compare string operations vs pre-compiled lookup."""
    print("\n" + "=" * 70)
    print(" BENCHMARK 3: SYMBOL LOOKUP SPEED")
    print("=" * 70)
    
    # Simulate the mappings
    symbols = [f"SYM{i}/USDT" for i in range(100)]
    
    # Pre-compile mappings (optimized)
    stream_to_symbol: Dict[str, str] = {}
    symbol_to_ws: Dict[str, str] = {}
    
    for symbol in symbols:
        clean_lower = symbol.replace("/", "").replace("-", "").lower()
        clean_upper = clean_lower.upper()
        stream_key = f"{clean_lower}@depth20@100ms"
        stream_to_symbol[stream_key] = symbol
        symbol_to_ws[clean_upper] = symbol
    
    # Test streams (as they come from Binance)
    test_streams = [f"sym{i}usdt@depth20@100ms" for i in range(100)]
    
    # OLD method: string split + upper on every message
    def lookup_old():
        results = []
        for stream in test_streams:
            ws_symbol = stream.split("@")[0].upper()
            symbol = symbol_to_ws.get(ws_symbol)
            results.append(symbol)
        return results
    
    # NEW method: direct O(1) lookup
    def lookup_new():
        results = []
        for stream in test_streams:
            symbol = stream_to_symbol.get(stream)
            results.append(symbol)
        return results
    
    print("\n  Testing 100 symbol lookups, 10,000 iterations...")
    
    result_old = benchmark(lookup_old, iterations=10000)
    result_new = benchmark(lookup_new, iterations=10000)
    
    print_benchmark_result("String split + upper (original)", result_old)
    print_benchmark_result("Direct dict lookup (optimized)", result_new)
    compare_results("Original", result_old, "Optimized", result_new)
    
    # Verify correctness
    old_results = lookup_old()
    new_results = lookup_new()
    matches = sum(1 for o, n in zip(old_results, new_results) if o == n)
    print(f"\n  ✅ Correctness: {matches}/100 lookups match")
    
    return result_old, result_new


# ============================================================
# BENCHMARK 4: get_recent() WITH ISLICE
# ============================================================

def benchmark_get_recent():
    """Compare list slicing vs islice for get_recent()."""
    print("\n" + "=" * 70)
    print(" BENCHMARK 4: get_recent() OPTIMIZATION")
    print("=" * 70)
    
    from itertools import islice
    
    # Create a deque with many snapshots
    bids, asks = generate_binance_data(20)
    timestamp = _now_ms()
    
    snapshots: deque = deque(maxlen=1000)
    for i in range(1000):
        snap = OrderBookSnapshot.from_binance_fast(
            "BTC/USDT", bids, asks, timestamp + i
        )
        snapshots.append(snap)
    
    # OLD method: list()[-n:]
    def get_recent_old(n: int = 50):
        return list(snapshots)[-n:]
    
    # NEW method: islice
    def get_recent_new(n: int = 50):
        length = len(snapshots)
        if length <= n:
            return list(snapshots)
        start = length - n
        return list(islice(snapshots, start, None))
    
    print("\n  Testing get_recent(50) from 1000 snapshots, 10,000 iterations...")
    
    result_old = benchmark(get_recent_old, iterations=10000)
    result_new = benchmark(get_recent_new, iterations=10000)
    
    print_benchmark_result("list()[-n:] (original)", result_old)
    print_benchmark_result("islice (optimized)", result_new)
    compare_results("Original", result_old, "Optimized", result_new)
    
    # Test with different sizes
    print("\n  Testing different window sizes:")
    for n in [10, 50, 100, 500]:
        def old(): return list(snapshots)[-n:]
        def new(): 
            length = len(snapshots)
            if length <= n:
                return list(snapshots)
            return list(islice(snapshots, length - n, None))
        
        r_old = benchmark(old, iterations=5000, warmup=50)
        r_new = benchmark(new, iterations=5000, warmup=50)
        speedup = r_old['mean_ms'] / r_new['mean_ms']
        print(f"    n={n:3d}: old={r_old['mean_ms']:.4f}ms, new={r_new['mean_ms']:.4f}ms, speedup={speedup:.2f}x")
    
    return result_old, result_new


# ============================================================
# BENCHMARK 5: FULL MESSAGE PROCESSING
# ============================================================

def benchmark_message_processing():
    """Benchmark complete message processing pipeline."""
    print("\n" + "=" * 70)
    print(" BENCHMARK 5: FULL MESSAGE PROCESSING PIPELINE")
    print("=" * 70)
    
    # Simulate a Binance combined stream message
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
    
    # Pre-compile mappings
    symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]
    stream_to_symbol = {
        f"{s.lower().replace('/', '')}@depth20@100ms": s 
        for s in symbols
    }
    symbol_to_ws = {
        s.replace("/", "").upper(): s 
        for s in symbols
    }
    
    # Generate test messages
    messages = [generate_message(random.choice(symbols)) for _ in range(100)]
    
    # OLD processing (string ops + from_raw)
    def process_old():
        results = []
        for msg in messages:
            stream = msg.get("stream", "")
            payload = msg.get("data", {})
            
            # String operations on every message
            ws_symbol = stream.split("@")[0].upper()
            symbol = symbol_to_ws.get(ws_symbol)
            if not symbol:
                continue
            
            bids = payload.get("bids", [])
            asks = payload.get("asks", [])
            timestamp = payload.get("E", _now_ms())
            
            # Convert to tuples for from_raw
            bids_tuples = [(float(p), float(s)) for p, s in bids]
            asks_tuples = [(float(p), float(s)) for p, s in asks]
            
            snapshot = OrderBookSnapshot.from_raw(
                symbol=symbol,
                bids=bids_tuples,
                asks=asks_tuples,
                timestamp=timestamp,
                exchange="binance"
            )
            results.append(snapshot)
        return results
    
    # NEW processing (pre-compiled + from_binance_fast)
    def process_new():
        results = []
        for msg in messages:
            stream = msg.get("stream", "")
            payload = msg.get("data", {})
            
            # O(1) direct lookup
            symbol = stream_to_symbol.get(stream)
            if not symbol:
                continue
            
            bids = payload.get("bids", [])
            asks = payload.get("asks", [])
            timestamp = payload.get("E", _now_ms())
            
            # Fast path
            snapshot = OrderBookSnapshot.from_binance_fast(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=timestamp,
                sequence=payload.get("lastUpdateId", 0)
            )
            results.append(snapshot)
        return results
    
    print("\n  Testing 100 messages per batch, 1,000 iterations...")
    
    result_old = benchmark(process_old, iterations=1000, warmup=50)
    result_new = benchmark(process_new, iterations=1000, warmup=50)
    
    print_benchmark_result("Original pipeline", result_old)
    print_benchmark_result("Optimized pipeline", result_new)
    compare_results("Original", result_old, "Optimized", result_new)
    
    # Calculate messages per second
    old_msg_per_sec = 100 * result_old['ops_per_sec']
    new_msg_per_sec = 100 * result_new['ops_per_sec']
    
    print(f"\n  📊 Throughput:")
    print(f"    Original:  {old_msg_per_sec:,.0f} messages/sec")
    print(f"    Optimized: {new_msg_per_sec:,.0f} messages/sec")
    
    return result_old, result_new


# ============================================================
# BENCHMARK 6: LIVE THROUGHPUT TEST
# ============================================================

def benchmark_live(duration: int = 10):
    """Test actual throughput with live WebSocket (if available)."""
    print("\n" + "=" * 70)
    print(f" BENCHMARK 6: LIVE THROUGHPUT TEST ({duration}s)")
    print("=" * 70)
    
    try:
        from oracle_orderbook import OrderBookManager, HAS_WEBSOCKETS
        
        if not HAS_WEBSOCKETS:
            print("\n  ⚠️ websockets not installed - skipping live test")
            return None
        
        manager = OrderBookManager(multi_symbol=True, store_history=False)
        
        stats = {
            "count": 0,
            "start_time": 0,
            "latencies": [],
        }
        
        def on_update(snap):
            stats["count"] += 1
            if snap.timestamp > 0:
                latency = _now_ms() - snap.timestamp
                stats["latencies"].append(latency)
        
        symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]
        print(f"\n  Subscribing to {len(symbols)} symbols...")
        
        manager.subscribe_many(symbols, on_update)
        stats["start_time"] = time.time()
        
        print(f"  Running for {duration} seconds...")
        time.sleep(duration)
        
        elapsed = time.time() - stats["start_time"]
        manager.stop()
        
        # Calculate stats
        msg_per_sec = stats["count"] / elapsed
        avg_latency = sum(stats["latencies"]) / len(stats["latencies"]) if stats["latencies"] else 0
        
        print(f"\n  📊 Results:")
        print(f"    Total messages: {stats['count']:,}")
        print(f"    Throughput:     {msg_per_sec:,.1f} messages/sec")
        print(f"    Avg latency:    {avg_latency:.1f} ms")
        
        if stats["latencies"]:
            sorted_lat = sorted(stats["latencies"])
            p50 = sorted_lat[len(sorted_lat) // 2]
            p99 = sorted_lat[int(len(sorted_lat) * 0.99)]
            print(f"    P50 latency:    {p50:.1f} ms")
            print(f"    P99 latency:    {p99:.1f} ms")
        
        return stats
        
    except Exception as e:
        print(f"\n  ❌ Error: {e}")
        return None


# ============================================================
# MAIN
# ============================================================

def run_all_benchmarks():
    """Run all benchmarks."""
    print("\n" + "=" * 70)
    print(" ORACLE ORDER BOOK - PERFORMANCE BENCHMARKS")
    print("=" * 70)
    print(f" Python: {sys.version.split()[0]}")
    print(f" Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # Run benchmarks
    results['parsing'] = benchmark_parsing()
    results['memory'] = benchmark_memory()
    results['lookup'] = benchmark_lookup()
    results['get_recent'] = benchmark_get_recent()
    results['pipeline'] = benchmark_message_processing()
    
    # Summary
    print("\n" + "=" * 70)
    print(" SUMMARY")
    print("=" * 70)
    
    if results['parsing']:
        old, new = results['parsing']
        print(f"\n  Parsing:          {old['mean_ms']/new['mean_ms']:.1f}x faster")
    
    if results['lookup']:
        old, new = results['lookup']
        print(f"  Symbol lookup:    {old['mean_ms']/new['mean_ms']:.1f}x faster")
    
    if results['get_recent']:
        old, new = results['get_recent']
        print(f"  get_recent():     {old['mean_ms']/new['mean_ms']:.1f}x faster")
    
    if results['pipeline']:
        old, new = results['pipeline']
        print(f"  Full pipeline:    {old['mean_ms']/new['mean_ms']:.1f}x faster")
    
    print("\n" + "=" * 70)
    print(" ✅ All benchmarks complete!")
    print("=" * 70)
    
    return results


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        
        if cmd == "parsing":
            benchmark_parsing()
        elif cmd == "memory":
            benchmark_memory()
        elif cmd == "lookup":
            benchmark_lookup()
        elif cmd == "recent":
            benchmark_get_recent()
        elif cmd == "pipeline":
            benchmark_message_processing()
        elif cmd == "live":
            duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
            benchmark_live(duration)
        elif cmd == "all":
            run_all_benchmarks()
        else:
            print(__doc__)
    else:
        run_all_benchmarks()


if __name__ == "__main__":
    main()
