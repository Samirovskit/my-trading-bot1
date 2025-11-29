#!/usr/bin/env python3
"""
oracle_orderbook.py - PRODUCTION ORDER BOOK v4.2 (CORRECTED)

Complete order book management system for cryptocurrency trading.
Usage test:
    python3 oracle_orderbook.py                                         # Show help
    python3 oracle_orderbook.py unit                                 # Run unit tests (no network needed)
    python3 oracle_orderbook.py stats                               # Test connection configuration
    python3 oracle_orderbook.py limits                              # Test capacity limits
    python3 oracle_orderbook.py live [sec]                        # Single symbol live test (default 30s)
    python3 oracle_orderbook.py multi [sec] [pairs]        # Multi-symbol test
    python3 oracle_orderbook.py health                            # Health monitoring
    python3 oracle_orderbook.py stress [sec]                  # Maximum symbols stress test
    python3 oracle_orderbook.py reconnect [sec]           # Reconnection stability test
    python3 oracle_orderbook.py backtest                       # Backtest simulation test
    python3 oracle_orderbook.py all                                   # Run everything
    python3 test_orderbook_full.py                                     # Run comprehensive tests
   python3 test_orderbook_full.py --quick                        # Quick unit tests only (no network)
   python3 test_orderbook_full.py --no-live                      # Skip live tests
   python3 test_orderbook_full.py --no-perf                    # Skip performance tests
   
1️⃣screen -S orderbook_stress     # Run 24-hour stress test     python3 oracle_orderbook.py stress 86400   # Detach: Ctrl+A, then D 
                                                             # Reconnect later                    screen -r orderbook_stress
2️⃣# Install memory profiler              pip install memory-profiler
    # Create memory test script
cat > test_memory.py << 'EOF'
from memory_profiler import profile
from oracle_orderbook import *
import time
@profile
def test_memory():
    manager = OrderBookManager(multi_symbol=True, store_history=False)
    manager.subscribe_many(ALL_TRADING_SYMBOLS[:50])    
    print("Running for 60 seconds...")
    time.sleep(60)    
    print("Stopping...")
    manager.stop()
if __name__ == "__main__":
    test_memory()
EOF

    # Run with profiling        python3 -m memory_profiler test_memory.py
    # Add at top of script for verbose logging
import logging
logging.getLogger('oracle_orderbook').setLevel(logging.DEBUG)

Features:
- Real-time WebSocket streaming (Binance, CCXT Pro)
- Synthetic order book generation from ticks/candles
- Order flow tracking and analysis
- Execution simulation with slippage modeling
- Backtest support with historical replay
- Health monitoring and alerting

Usage:
    # Live trading
    manager = OrderBookManager()
    manager.subscribe("BTC/USDT", callback)
    
    # Backtesting
    manager = OrderBookManager(backtest_mode=True)
    manager.load_backtest_candles("BTC/USDT", candles)
    snapshot = manager.get_backtest_snapshot("BTC/USDT", timestamp)

Author: Oracle Trading System
Version: 4.2
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import pickle
import random
import re
import sys
import threading
import time
import warnings
from abc import ABC, abstractmethod
from itertools import islice
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Final,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

# Suppress resource warnings during shutdown
warnings.filterwarnings("ignore", category=ResourceWarning)


# ============================================================
# OPTIONAL DEPENDENCIES
# ============================================================

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    websockets = None

    class ConnectionClosed(Exception):
        """Placeholder for missing websockets dependency."""
        pass


try:
    import ccxt.pro as ccxtpro
    HAS_CCXT_PRO = True
except ImportError:
    HAS_CCXT_PRO = False
    ccxtpro = None


# ============================================================
# LOGGING CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger('asyncio').setLevel(logging.CRITICAL)
logging.getLogger('websockets').setLevel(logging.WARNING)


# ============================================================
# TYPE ALIASES
# ============================================================

# Price and size tuple for order book levels
PriceSize = Tuple[float, float]

# Tick data: (timestamp_ms, price) - for price stream without size
TickData = Tuple[int, float]

# Full trade data: (timestamp_ms, price, size) - for complete trade records
TradeData = Tuple[int, float, float]

# Simple trade: (price, size) - for quick processing without timestamp
SimpleTrade = Tuple[float, float]

# Callback type for order book updates
Callback = Callable[['OrderBookSnapshot'], None]


def _now_ms() -> int:
    """Get current time in milliseconds since epoch."""
    return int(time.time() * 1000)


# ============================================================
# CONSTANTS
# ============================================================

class OrderBookConstants:
    """All configuration constants in one place."""

    # Synthetic Order Book Generation
    DEFAULT_SPREAD_BPS: Final[float] = 5.0
    MIN_SPREAD_BPS: Final[float] = 2.0
    MAX_SPREAD_MULT: Final[float] = 3.0
    VOLATILITY_SPREAD_MULT: Final[float] = 5.0
    DEFAULT_BASE_SIZE: Final[float] = 10.0
    DEFAULT_SIZE_DECAY: Final[float] = 0.7
    DEFAULT_LEVELS: Final[int] = 10

    # Signal Generation Weights
    SIGNAL_IMBALANCE_WEIGHT: Final[float] = 0.4
    SIGNAL_DEPTH_WEIGHT: Final[float] = 0.4
    SIGNAL_TREND_WEIGHT: Final[float] = 0.2
    SIGNAL_TREND_SCALE: Final[float] = 10.0

    # WebSocket Configuration
    WS_PING_INTERVAL: Final[int] = 20
    WS_PING_TIMEOUT: Final[int] = 10
    WS_CLOSE_TIMEOUT: Final[int] = 5
    WS_RECV_TIMEOUT: Final[int] = 30
    WS_MAX_MESSAGE_SIZE: Final[int] = 10 * 1024 * 1024  # 10MB

    # Reconnection Settings
    MAX_RECONNECT_ATTEMPTS: Final[int] = 10
    INITIAL_BACKOFF: Final[float] = 1.0
    MAX_BACKOFF: Final[float] = 60.0
    SHUTDOWN_TIMEOUT: Final[float] = 3.0

    # History Storage
    HISTORY_FLUSH_SIZE: Final[int] = 1000
    MAX_SNAPSHOTS_DEFAULT: Final[int] = 100
    MAX_FLUSH_THREADS: Final[int] = 4

    # Health & Validation
    STALE_DATA_MS: Final[int] = 5000
    MAX_SPREAD_BPS_ALERT: Final[float] = 500.0
    MAX_PRICE_SPIKE_PCT: Final[float] = 5.0
    HEALTH_REPORT_INTERVAL: Final[float] = 120.0
    HEALTH_CALC_INTERVAL: Final[float] = 10.0
    ALERT_THROTTLE_SECONDS: Final[int] = 60
    MAX_THROTTLE_KEYS: Final[int] = 10000
    MAX_VALIDATION_FAILURES: Final[int] = 100

    # Connection Limits
    MAX_URL_LENGTH: Final[int] = 2000
    CCXT_MIN_WATCH_INTERVAL: Final[float] = 0.05
    CONNECTION_CHECK_INTERVAL: Final[float] = 1.0

    # Symbol Validation Pattern (uppercase with slash separator)
    SYMBOL_PATTERN: Final[re.Pattern] = re.compile(r'^[A-Z0-9]+/[A-Z0-9]+$')

    # Execution Simulation Defaults
    DEFAULT_FEE_BPS: Final[float] = 4.0  # 0.04% taker fee
    DEFAULT_SLIPPAGE_BPS: Final[float] = 2.0
    LARGE_TRADE_THRESHOLD: Final[float] = 1.0  # BTC equivalent
    DEFAULT_LIQUIDITY_PENALTY_PCT: Final[float] = 2.0  # For insufficient liquidity


class TradeSide(Enum):
    """Trade direction for tick classification."""
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class AlertLevel(Enum):
    """Alert severity levels."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# ============================================================
# STREAM HEALTH
# ============================================================

@dataclass
class StreamHealth:
    # ... existing fields ...

    @property
    def is_healthy(self) -> bool:
        """
        Check if stream is in healthy state.
        
        Healthy means:
        - Connected
        - Recent data (< 10 seconds old)
        - Low latency (< 1 second)
        - Few errors (< 10)
        """
        # Add grace period for new connections
        if self.last_update_ms == 0:
            # No data yet - give it time before marking unhealthy
            return self.connected and self.error_count < 10
        
        return (
            self.connected
            and self.age_ms < 10000
            and self.latency_ms < 1000
            and self.error_count < 10
        )
        
    connected: bool = False
    last_update_ms: int = 0
    updates_per_second: float = 0.0
    sequence_gaps: int = 0
    reconnect_count: int = 0
    latency_ms: float = 0.0
    error_count: int = 0
    last_error: str = ""

    @property
    def age_ms(self) -> int:
        """Milliseconds since last update."""
        if self.last_update_ms == 0:
            return 999999
        return _now_ms() - self.last_update_ms

    @property
    def is_healthy(self) -> bool:
        """
        Check if stream is in healthy state.
        
        Healthy means:
        - Connected
        - Recent data (< 10 seconds old)
        - Low latency (< 1 second)
        - Few errors (< 10)
        """
        return (
            self.connected
            and self.age_ms < 10000
            and self.latency_ms < 1000
            and self.error_count < 10
        )

    def record_error(self, error: str) -> None:
        """Record an error occurrence."""
        self.error_count += 1
        self.last_error = str(error)[:200]  # Truncate long errors

    def record_reconnect(self) -> None:
        """Record a reconnection attempt."""
        self.reconnect_count += 1

    def reset_errors(self) -> None:
        """Clear error state after recovery."""
        self.error_count = 0
        self.last_error = ""

    def to_dict(self) -> Dict[str, Any]:
        """Export health metrics as dictionary."""
        return {
            "connected": self.connected,
            "age_ms": self.age_ms,
            "updates_per_second": round(self.updates_per_second, 2),
            "sequence_gaps": self.sequence_gaps,
            "reconnect_count": self.reconnect_count,
            "latency_ms": round(self.latency_ms, 2),
            "error_count": self.error_count,
            "last_error": self.last_error,
            "is_healthy": self.is_healthy,
        }


# ============================================================
# ORDER BOOK LEVEL
# ============================================================

@dataclass(slots=True)
class OrderBookLevel:
    """
    A single price level in the order book.
    
    Represents a price point with available size (liquidity).
    Uses slots for memory efficiency.
    
    Attributes:
        price: The price at this level (must be positive)
        size: The available size/quantity (must be non-negative)
    """
    price: float
    size: float

    def __post_init__(self) -> None:
        """Validate and convert types after initialization."""
        # Convert to float (handles string inputs from APIs)
        try:
            self.price = float(self.price)
            self.size = float(self.size)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Invalid level: price={self.price}, size={self.size}"
            ) from e

        # Validate values
        if self.price <= 0:
            raise ValueError(f"Price must be positive: {self.price}")
        if self.size < 0:
            raise ValueError(f"Size cannot be negative: {self.size}")

    @property
    def value(self) -> float:
        """Notional value (price × size)."""
        return self.price * self.size

    def to_tuple(self) -> PriceSize:
        """Export as (price, size) tuple."""
        return (self.price, self.size)

    def __repr__(self) -> str:
        return f"Level({self.price:.6f} × {self.size:.4f})"


# ============================================================
# ♦️ORDER BOOK SNAPSHOT
# ============================================================
@dataclass(slots=True)
class OrderBookSnapshot:
    """
    Immutable snapshot of an order book at a point in time.
    
    Provides comprehensive order book metrics including:
    - Basic: mid price, spread, best bid/ask
    - Imbalance: top-of-book and multi-level
    - Advanced pricing: VWAP, microprice, weighted mid
    - Liquidity: depth, market impact, wall detection
    
    Use from_raw() for unsorted data (automatic sorting).
    Use from_sorted() for pre-sorted data (faster, no validation).
    Use from_binance_fast() for Binance streams (fastest, trusted data).
    
    Attributes:
        symbol: Trading pair symbol (e.g., "BTC/USDT")
        timestamp: Snapshot timestamp in milliseconds
        bids: List of bid levels, sorted high to low
        asks: List of ask levels, sorted low to high
        sequence: Exchange sequence number for gap detection
        exchange: Exchange identifier
    """
    symbol: str
    timestamp: int
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    sequence: int = 0
    exchange: str = ""
    _sorted: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Sort and filter levels if needed."""
        if not self._sorted:
            # Filter zero-size levels and sort
            self.bids = sorted(
                (b for b in (self.bids or []) if b.size > 0),
                key=lambda x: x.price,
                reverse=True  # Highest bid first
            )
            self.asks = sorted(
                (a for a in (self.asks or []) if a.size > 0),
                key=lambda x: x.price  # Lowest ask first
            )
            self._sorted = True

    @staticmethod
    def _parse_levels(raw: Optional[List[PriceSize]]) -> List[OrderBookLevel]:
        """
        Parse raw price/size tuples into OrderBookLevel objects.
        
        Handles various input formats from different exchanges.
        Silently skips invalid entries.
        """
        if not raw:
            return []

        levels = []
        for item in raw:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    price = float(item[0])
                    size = float(item[1])
                    if price > 0 and size >= 0:
                        levels.append(OrderBookLevel(price, size))
            except (ValueError, IndexError, TypeError):
                continue  # Skip invalid entries
        return levels

    @classmethod
    def from_raw(
        cls,
        symbol: str,
        bids: List[PriceSize],
        asks: List[PriceSize],
        timestamp: Optional[int] = None,
        sequence: int = 0,
        exchange: str = ""
    ) -> 'OrderBookSnapshot':
        """
        Create snapshot from raw price/size lists.
        
        Automatically parses, validates, and sorts the data.
        
        Args:
            symbol: Trading pair symbol
            bids: List of [price, size] for bids
            asks: List of [price, size] for asks
            timestamp: Snapshot timestamp (defaults to now)
            sequence: Exchange sequence number
            exchange: Exchange identifier
            
        Returns:
            OrderBookSnapshot instance
        """
        return cls(
            symbol=symbol,
            timestamp=timestamp or _now_ms(),
            bids=cls._parse_levels(bids),
            asks=cls._parse_levels(asks),
            sequence=sequence,
            exchange=exchange,
            _sorted=False  # Will be sorted in __post_init__
        )

    @classmethod
    def from_sorted(
        cls,
        symbol: str,
        bids: List[OrderBookLevel],
        asks: List[OrderBookLevel],
        timestamp: Optional[int] = None,
        sequence: int = 0,
        exchange: str = ""
    ) -> 'OrderBookSnapshot':
        """
        Create snapshot from pre-sorted OrderBookLevel lists.
        
        Faster than from_raw() as it skips parsing and sorting.
        Caller must ensure data is already sorted correctly.
        
        Args:
            symbol: Trading pair symbol
            bids: Pre-sorted bid levels (high to low)
            asks: Pre-sorted ask levels (low to high)
            timestamp: Snapshot timestamp (defaults to now)
            sequence: Exchange sequence number
            exchange: Exchange identifier
            
        Returns:
            OrderBookSnapshot instance
        """
        # Bypass __post_init__ sorting by creating instance directly
        snapshot = object.__new__(cls)
        snapshot.symbol = symbol
        snapshot.timestamp = timestamp or _now_ms()
        snapshot.bids = bids
        snapshot.asks = asks
        snapshot.sequence = sequence
        snapshot.exchange = exchange
        snapshot._sorted = True
        return snapshot

    @classmethod
    def from_binance_fast(
        cls,
        symbol: str,
        bids: List[List[str]],
        asks: List[List[str]],
        timestamp: int,
        sequence: int = 0
    ) -> 'OrderBookSnapshot':
        """
        Ultra-fast path for Binance depth streams.
        
        Optimized for Binance's guaranteed data format:
        - Pre-sorted (bids descending, asks ascending)
        - String price/size pairs
        - Max 20 levels for depth20 streams
        
        Skips ALL validation for maximum speed.
        Only use with trusted Binance WebSocket data.
        
        Args:
            symbol: Trading pair symbol
            bids: Binance bid data [[price_str, size_str], ...]
            asks: Binance ask data [[price_str, size_str], ...]
            timestamp: Event timestamp from Binance
            sequence: lastUpdateId from Binance
            
        Returns:
            OrderBookSnapshot instance
        """
        # Limit to 20 levels (Binance depth20 guarantee)
        n_bids = min(20, len(bids))
        n_asks = min(20, len(asks))
        
        # Pre-allocate lists with exact size
        parsed_bids: List[OrderBookLevel] = [None] * n_bids  # type: ignore
        parsed_asks: List[OrderBookLevel] = [None] * n_asks  # type: ignore
        
        # Direct object creation bypassing __init__ and __post_init__
        for i in range(n_bids):
            level = object.__new__(OrderBookLevel)
            level.price = float(bids[i][0])
            level.size = float(bids[i][1])
            parsed_bids[i] = level
        
        for i in range(n_asks):
            level = object.__new__(OrderBookLevel)
            level.price = float(asks[i][0])
            level.size = float(asks[i][1])
            parsed_asks[i] = level
        
        # Create snapshot bypassing __post_init__
        snapshot = object.__new__(cls)
        snapshot.symbol = symbol
        snapshot.timestamp = timestamp
        snapshot.bids = parsed_bids
        snapshot.asks = parsed_asks
        snapshot.sequence = sequence
        snapshot.exchange = "binance"
        snapshot._sorted = True
        
        return snapshot

    # ─────────────────────────────────────────────────────────
    # VALIDATION PROPERTIES
    # ─────────────────────────────────────────────────────────

    @property
    def is_valid(self) -> bool:
        """Check if order book has both valid bids and asks."""
        return bool(
            self.bids
            and self.asks
            and self.bids[0].price > 0
            and self.asks[0].price > 0
        )

    @property
    def is_crossed(self) -> bool:
        """
        Check if best bid >= best ask (invalid state).
        
        A crossed book indicates a data error or arbitrage opportunity.
        """
        return self.is_valid and self.bids[0].price >= self.asks[0].price

    def age_ms(self) -> int:
        """Milliseconds since snapshot was taken."""
        return _now_ms() - self.timestamp

    def is_stale(self, max_age_ms: int = OrderBookConstants.STALE_DATA_MS) -> bool:
        """Check if data is too old for trading."""
        return self.age_ms() > max_age_ms

    # ─────────────────────────────────────────────────────────
    # BASIC METRICS
    # ─────────────────────────────────────────────────────────

    @property
    def best_bid(self) -> float:
        """Highest bid price (best price to sell at)."""
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        """Lowest ask price (best price to buy at)."""
        return self.asks[0].price if self.asks else 0.0

    @property
    def best_bid_size(self) -> float:
        """Size available at best bid."""
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        """Size available at best ask."""
        return self.asks[0].size if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        """Mid-market price: (best_bid + best_ask) / 2."""
        if not self.is_valid:
            return 0.0
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> float:
        """Absolute spread: best_ask - best_bid."""
        if not self.is_valid:
            return 0.0
        return self.asks[0].price - self.bids[0].price

    @property
    def spread_pct(self) -> float:
        """Spread as percentage of mid price."""
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return (self.spread / mid) * 100

    @property
    def spread_bps(self) -> float:
        """Spread in basis points (1 bp = 0.01%)."""
        return self.spread_pct * 100

    # ─────────────────────────────────────────────────────────
    # IMBALANCE METRICS
    # ─────────────────────────────────────────────────────────

    @property
    def imbalance(self) -> float:
        """
        Top-of-book imbalance based on size at best bid/ask.
        
        Returns:
            -1.0 (all asks, bearish) to +1.0 (all bids, bullish)
        """
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return 0.0
        return (self.best_bid_size - self.best_ask_size) / total

    def depth_imbalance(self, levels: int = 10) -> float:
        """
        Multi-level volume imbalance.
        
        Args:
            levels: Number of levels to consider
            
        Returns:
            -1.0 (ask heavy, bearish) to +1.0 (bid heavy, bullish)
        """
        bid_vol = sum(b.size for b in self.bids[:levels])
        ask_vol = sum(a.size for a in self.asks[:levels])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def value_imbalance(self, levels: int = 10) -> float:
        """
        Multi-level notional value imbalance.
        
        Weights by price × size instead of just size.
        
        Args:
            levels: Number of levels to consider
            
        Returns:
            -1.0 (ask heavy) to +1.0 (bid heavy)
        """
        bid_val = sum(b.value for b in self.bids[:levels])
        ask_val = sum(a.value for a in self.asks[:levels])
        total = bid_val + ask_val
        if total == 0:
            return 0.0
        return (bid_val - ask_val) / total

    # ─────────────────────────────────────────────────────────
    # VOLUME & VALUE
    # ─────────────────────────────────────────────────────────

    def total_bid_volume(self, levels: int = 10) -> float:
        """Total bid volume across specified levels."""
        return sum(b.size for b in self.bids[:levels])

    def total_ask_volume(self, levels: int = 10) -> float:
        """Total ask volume across specified levels."""
        return sum(a.size for a in self.asks[:levels])

    def total_bid_value(self, levels: int = 10) -> float:
        """Total bid notional value across specified levels."""
        return sum(b.value for b in self.bids[:levels])

    def total_ask_value(self, levels: int = 10) -> float:
        """Total ask notional value across specified levels."""
        return sum(a.value for a in self.asks[:levels])

    # ─────────────────────────────────────────────────────────
    # ADVANCED PRICING
    # ─────────────────────────────────────────────────────────

    def vwap_bid(self, levels: int = 5) -> float:
        """
        Volume-weighted average bid price.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            VWAP of bid side
        """
        bids = self.bids[:levels]
        if not bids:
            return 0.0
        total_size = sum(b.size for b in bids)
        if total_size == 0:
            return self.best_bid
        return sum(b.price * b.size for b in bids) / total_size

    def vwap_ask(self, levels: int = 5) -> float:
        """
        Volume-weighted average ask price.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            VWAP of ask side
        """
        asks = self.asks[:levels]
        if not asks:
            return 0.0
        total_size = sum(a.size for a in asks)
        if total_size == 0:
            return self.best_ask
        return sum(a.price * a.size for a in asks) / total_size

    def weighted_mid_price(self, levels: int = 5) -> float:
        """
        Volume-weighted mid price.
        
        Weights each side's VWAP by the opposite side's volume,
        giving more weight to the side with less liquidity.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            Weighted mid price
        """
        if not self.is_valid:
            return 0.0
        bid_vol = self.total_bid_volume(levels)
        ask_vol = self.total_ask_volume(levels)
        total = bid_vol + ask_vol
        if total == 0:
            return self.mid_price
        return (
            self.vwap_bid(levels) * ask_vol +
            self.vwap_ask(levels) * bid_vol
        ) / total

    def microprice(self) -> float:
        """
        Size-weighted mid price (microstructure fair price).
        
        Skews toward the side with less liquidity, as that side
        is more likely to move. Used in HFT for fair value estimation.
        
        Returns:
            Microprice estimate
        """
        if not self.is_valid:
            return 0.0
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return self.mid_price
        # Weight bid by ask size and vice versa
        return (
            self.best_bid * self.best_ask_size +
            self.best_ask * self.best_bid_size
        ) / total

    # ─────────────────────────────────────────────────────────
    # WALL DETECTION
    # ─────────────────────────────────────────────────────────

    def find_walls(
        self,
        threshold_mult: float = 3.0,
        levels: int = 20,
        min_size: float = 0.0
    ) -> Dict[str, List[OrderBookLevel]]:
        """
        Find significant order walls (large resting orders).
        
        Walls can act as support (bid walls) or resistance (ask walls).
        
        Args:
            threshold_mult: Multiplier over average size to qualify as wall
            levels: Number of levels to scan
            min_size: Minimum absolute size to qualify
            
        Returns:
            Dict with 'bid_walls' and 'ask_walls' lists
        """
        result: Dict[str, List[OrderBookLevel]] = {
            "bid_walls": [],
            "ask_walls": []
        }
        max_levels = min(levels, 20)

        bids = self.bids[:max_levels]
        asks = self.asks[:max_levels]

        # Find bid walls (support)
        if len(bids) >= 3:
            avg_size = sum(b.size for b in bids) / len(bids)
            threshold = max(avg_size * threshold_mult, min_size)
            result["bid_walls"] = [b for b in bids if b.size >= threshold]

        # Find ask walls (resistance)
        if len(asks) >= 3:
            avg_size = sum(a.size for a in asks) / len(asks)
            threshold = max(avg_size * threshold_mult, min_size)
            result["ask_walls"] = [a for a in asks if a.size >= threshold]

        return result

    def nearest_bid_wall(
        self,
        threshold_mult: float = 3.0
    ) -> Optional[OrderBookLevel]:
        """Find nearest significant bid wall (support level)."""
        walls = self.find_walls(threshold_mult)
        return walls["bid_walls"][0] if walls["bid_walls"] else None

    def nearest_ask_wall(
        self,
        threshold_mult: float = 3.0
    ) -> Optional[OrderBookLevel]:
        """Find nearest significant ask wall (resistance level)."""
        walls = self.find_walls(threshold_mult)
        return walls["ask_walls"][0] if walls["ask_walls"] else None

    # ─────────────────────────────────────────────────────────
    # LIQUIDITY ANALYSIS
    # ─────────────────────────────────────────────────────────

    def liquidity_at_price(
        self,
        price: float,
        tolerance_pct: float = 0.1
    ) -> Tuple[float, float]:
        """
        Find liquidity around a specific price.
        
        Args:
            price: Target price
            tolerance_pct: Price tolerance as percentage
            
        Returns:
            Tuple of (bid_liquidity, ask_liquidity)
        """
        if price <= 0:
            return 0.0, 0.0

        tol = price * (tolerance_pct / 100)
        bid_liq = sum(
            b.size for b in self.bids
            if price - tol <= b.price <= price + tol
        )
        ask_liq = sum(
            a.size for a in self.asks
            if price - tol <= a.price <= price + tol
        )
        return bid_liq, ask_liq

    def depth_at_distance(self, pct_from_mid: float = 1.0) -> Tuple[float, float]:
        """
        Get depth within percentage distance from mid price.
        
        Args:
            pct_from_mid: Percentage distance from mid price
            
        Returns:
            Tuple of (bid_depth, ask_depth)
        """
        mid = self.mid_price
        if mid <= 0:
            return 0.0, 0.0

        dist = mid * (pct_from_mid / 100)
        bid_depth = sum(b.size for b in self.bids if b.price >= mid - dist)
        ask_depth = sum(a.size for a in self.asks if a.price <= mid + dist)
        return bid_depth, ask_depth

    def market_impact(
        self,
        size: float,
        side: str = "buy",
        insufficient_liquidity_penalty_pct: float = OrderBookConstants.DEFAULT_LIQUIDITY_PENALTY_PCT
    ) -> float:
        """
        Estimate average fill price for a market order.
        
        Walks the order book to simulate execution and calculates
        the volume-weighted average price.
        
        Args:
            size: Order size to fill
            side: 'buy' or 'sell'
            insufficient_liquidity_penalty_pct: Penalty for unfilled portion
            
        Returns:
            Estimated average execution price
        """
        if size <= 0:
            return self.mid_price

        remaining = size
        total_cost = 0.0
        levels = self.asks if side == "buy" else self.bids

        # Walk through levels
        for level in levels:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            remaining -= fill

        # Handle insufficient liquidity
        if remaining > 0:
            if levels:
                # Extrapolate with penalty
                penalty_mult = insufficient_liquidity_penalty_pct / 100
                if side == "buy":
                    penalty_price = levels[-1].price * (1 + penalty_mult)
                else:
                    penalty_price = levels[-1].price * (1 - penalty_mult)
                total_cost += remaining * penalty_price
                logger.debug(
                    f"Insufficient liquidity: {remaining/size*100:.1f}% unfilled, "
                    f"extrapolating with {penalty_mult*100:+.1f}% penalty"
                )
            else:
                return self.mid_price

        return total_cost / size if size > 0 else self.mid_price

    # ─────────────────────────────────────────────────────────
    # SERIALIZATION
    # ─────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Export snapshot as dictionary for serialization."""
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "bids": [b.to_tuple() for b in self.bids[:50]],
            "asks": [a.to_tuple() for a in self.asks[:50]],
            "sequence": self.sequence,
            "exchange": self.exchange,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OrderBookSnapshot':
        """Create snapshot from dictionary."""
        return cls.from_raw(
            symbol=data.get("symbol", "UNKNOWN"),
            bids=data.get("bids", []),
            asks=data.get("asks", []),
            timestamp=data.get("timestamp"),
            sequence=data.get("sequence", 0),
            exchange=data.get("exchange", ""),
        )

    def __repr__(self) -> str:
        if not self.is_valid:
            return f"OrderBook({self.symbol} @ {self.timestamp} | INVALID)"
        return (
            f"OrderBook({self.symbol} @ {self.timestamp} | "
            f"bid={self.best_bid:.4f} ask={self.best_ask:.4f} "
            f"spread={self.spread_bps:.2f}bps imb={self.imbalance:+.2f})"
        )


# ─────────────────────────────────────────────────────────
#♦️ BASIC METRICS
# ─────────────────────────────────────────────────────────

    @property
    def best_bid(self) -> float:
        """Highest bid price (best price to sell at)."""
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        """Lowest ask price (best price to buy at)."""
        return self.asks[0].price if self.asks else 0.0

    @property
    def best_bid_size(self) -> float:
        """Size available at best bid."""
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        """Size available at best ask."""
        return self.asks[0].size if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        """Mid-market price: (best_bid + best_ask) / 2."""
        if not self.is_valid:
            return 0.0
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> float:
        """Absolute spread: best_ask - best_bid."""
        if not self.is_valid:
            return 0.0
        return self.asks[0].price - self.bids[0].price

    @property
    def spread_pct(self) -> float:
        """Spread as percentage of mid price."""
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return (self.spread / mid) * 100

    @property
    def spread_bps(self) -> float:
        """Spread in basis points (1 bp = 0.01%)."""
        return self.spread_pct * 100

    # ─────────────────────────────────────────────────────────
    # IMBALANCE METRICS
    # ─────────────────────────────────────────────────────────

    @property
    def imbalance(self) -> float:
        """
        Top-of-book imbalance based on size at best bid/ask.
        
        Returns:
            -1.0 (all asks, bearish) to +1.0 (all bids, bullish)
        """
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return 0.0
        return (self.best_bid_size - self.best_ask_size) / total

    def depth_imbalance(self, levels: int = 10) -> float:
        """
        Multi-level volume imbalance.
        
        Args:
            levels: Number of levels to consider
            
        Returns:
            -1.0 (ask heavy, bearish) to +1.0 (bid heavy, bullish)
        """
        bid_vol = sum(b.size for b in self.bids[:levels])
        ask_vol = sum(a.size for a in self.asks[:levels])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def value_imbalance(self, levels: int = 10) -> float:
        """
        Multi-level notional value imbalance.
        
        Weights by price × size instead of just size.
        
        Args:
            levels: Number of levels to consider
            
        Returns:
            -1.0 (ask heavy) to +1.0 (bid heavy)
        """
        bid_val = sum(b.value for b in self.bids[:levels])
        ask_val = sum(a.value for a in self.asks[:levels])
        total = bid_val + ask_val
        if total == 0:
            return 0.0
        return (bid_val - ask_val) / total

    # ─────────────────────────────────────────────────────────
    # VOLUME & VALUE
    # ─────────────────────────────────────────────────────────

    def total_bid_volume(self, levels: int = 10) -> float:
        """Total bid volume across specified levels."""
        return sum(b.size for b in self.bids[:levels])

    def total_ask_volume(self, levels: int = 10) -> float:
        """Total ask volume across specified levels."""
        return sum(a.size for a in self.asks[:levels])

    def total_bid_value(self, levels: int = 10) -> float:
        """Total bid notional value across specified levels."""
        return sum(b.value for b in self.bids[:levels])

    def total_ask_value(self, levels: int = 10) -> float:
        """Total ask notional value across specified levels."""
        return sum(a.value for a in self.asks[:levels])

    # ─────────────────────────────────────────────────────────
    # ADVANCED PRICING
    # ─────────────────────────────────────────────────────────

    def vwap_bid(self, levels: int = 5) -> float:
        """
        Volume-weighted average bid price.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            VWAP of bid side
        """
        bids = self.bids[:levels]
        if not bids:
            return 0.0
        total_size = sum(b.size for b in bids)
        if total_size == 0:
            return self.best_bid
        return sum(b.price * b.size for b in bids) / total_size

    def vwap_ask(self, levels: int = 5) -> float:
        """
        Volume-weighted average ask price.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            VWAP of ask side
        """
        asks = self.asks[:levels]
        if not asks:
            return 0.0
        total_size = sum(a.size for a in asks)
        if total_size == 0:
            return self.best_ask
        return sum(a.price * a.size for a in asks) / total_size

    def weighted_mid_price(self, levels: int = 5) -> float:
        """
        Volume-weighted mid price.
        
        Weights each side's VWAP by the opposite side's volume,
        giving more weight to the side with less liquidity.
        
        Args:
            levels: Number of levels to include
            
        Returns:
            Weighted mid price
        """
        if not self.is_valid:
            return 0.0
        bid_vol = self.total_bid_volume(levels)
        ask_vol = self.total_ask_volume(levels)
        total = bid_vol + ask_vol
        if total == 0:
            return self.mid_price
        return (
            self.vwap_bid(levels) * ask_vol +
            self.vwap_ask(levels) * bid_vol
        ) / total

    def microprice(self) -> float:
        """
        Size-weighted mid price (microstructure fair price).
        
        Skews toward the side with less liquidity, as that side
        is more likely to move. Used in HFT for fair value estimation.
        
        Returns:
            Microprice estimate
        """
        if not self.is_valid:
            return 0.0
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return self.mid_price
        # Weight bid by ask size and vice versa
        return (
            self.best_bid * self.best_ask_size +
            self.best_ask * self.best_bid_size
        ) / total

    # ─────────────────────────────────────────────────────────
    # WALL DETECTION
    # ─────────────────────────────────────────────────────────

    def find_walls(
        self,
        threshold_mult: float = 3.0,
        levels: int = 20,
        min_size: float = 0.0
    ) -> Dict[str, List[OrderBookLevel]]:
        """
        Find significant order walls (large resting orders).
        
        Walls can act as support (bid walls) or resistance (ask walls).
        
        Args:
            threshold_mult: Multiplier over average size to qualify as wall
            levels: Number of levels to scan
            min_size: Minimum absolute size to qualify
            
        Returns:
            Dict with 'bid_walls' and 'ask_walls' lists
        """
        result: Dict[str, List[OrderBookLevel]] = {
            "bid_walls": [],
            "ask_walls": []
        }
        max_levels = min(levels, 20)

        bids = self.bids[:max_levels]
        asks = self.asks[:max_levels]

        # Find bid walls (support)
        if len(bids) >= 3:
            avg_size = sum(b.size for b in bids) / len(bids)
            threshold = max(avg_size * threshold_mult, min_size)
            result["bid_walls"] = [b for b in bids if b.size >= threshold]

        # Find ask walls (resistance)
        if len(asks) >= 3:
            avg_size = sum(a.size for a in asks) / len(asks)
            threshold = max(avg_size * threshold_mult, min_size)
            result["ask_walls"] = [a for a in asks if a.size >= threshold]

        return result

    def nearest_bid_wall(
        self,
        threshold_mult: float = 3.0
    ) -> Optional[OrderBookLevel]:
        """Find nearest significant bid wall (support level)."""
        walls = self.find_walls(threshold_mult)
        return walls["bid_walls"][0] if walls["bid_walls"] else None

    def nearest_ask_wall(
        self,
        threshold_mult: float = 3.0
    ) -> Optional[OrderBookLevel]:
        """Find nearest significant ask wall (resistance level)."""
        walls = self.find_walls(threshold_mult)
        return walls["ask_walls"][0] if walls["ask_walls"] else None

    # ─────────────────────────────────────────────────────────
    # LIQUIDITY ANALYSIS
    # ─────────────────────────────────────────────────────────

    def liquidity_at_price(
        self,
        price: float,
        tolerance_pct: float = 0.1
    ) -> Tuple[float, float]:
        """
        Find liquidity around a specific price.
        
        Args:
            price: Target price
            tolerance_pct: Price tolerance as percentage
            
        Returns:
            Tuple of (bid_liquidity, ask_liquidity)
        """
        if price <= 0:
            return 0.0, 0.0

        tol = price * (tolerance_pct / 100)
        bid_liq = sum(
            b.size for b in self.bids
            if price - tol <= b.price <= price + tol
        )
        ask_liq = sum(
            a.size for a in self.asks
            if price - tol <= a.price <= price + tol
        )
        return bid_liq, ask_liq

    def depth_at_distance(self, pct_from_mid: float = 1.0) -> Tuple[float, float]:
        """
        Get depth within percentage distance from mid price.
        
        Args:
            pct_from_mid: Percentage distance from mid price
            
        Returns:
            Tuple of (bid_depth, ask_depth)
        """
        mid = self.mid_price
        if mid <= 0:
            return 0.0, 0.0

        dist = mid * (pct_from_mid / 100)
        bid_depth = sum(b.size for b in self.bids if b.price >= mid - dist)
        ask_depth = sum(a.size for a in self.asks if a.price <= mid + dist)
        return bid_depth, ask_depth

    def market_impact(
        self,
        size: float,
        side: str = "buy",
        insufficient_liquidity_penalty_pct: float = OrderBookConstants.DEFAULT_LIQUIDITY_PENALTY_PCT
    ) -> float:
        """
        Estimate average fill price for a market order.
        
        Walks the order book to simulate execution and calculates
        the volume-weighted average price.
        
        Args:
            size: Order size to fill
            side: 'buy' or 'sell'
            insufficient_liquidity_penalty_pct: Penalty for unfilled portion
            
        Returns:
            Estimated average execution price
        """
        if size <= 0:
            return self.mid_price

        remaining = size
        total_cost = 0.0
        levels = self.asks if side == "buy" else self.bids

        # Walk through levels
        for level in levels:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            remaining -= fill

        # Handle insufficient liquidity
        if remaining > 0:
            if levels:
                # Extrapolate with penalty
                penalty_mult = insufficient_liquidity_penalty_pct / 100
                if side == "buy":
                    penalty_price = levels[-1].price * (1 + penalty_mult)
                else:
                    penalty_price = levels[-1].price * (1 - penalty_mult)
                total_cost += remaining * penalty_price
                logger.debug(
                    f"Insufficient liquidity: {remaining/size*100:.1f}% unfilled, "
                    f"extrapolating with {penalty_mult*100:+.1f}% penalty"
                )
            else:
                return self.mid_price

        return total_cost / size if size > 0 else self.mid_price

    # ─────────────────────────────────────────────────────────
    # SERIALIZATION
    # ─────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Export snapshot as dictionary for serialization."""
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "bids": [b.to_tuple() for b in self.bids[:50]],
            "asks": [a.to_tuple() for a in self.asks[:50]],
            "sequence": self.sequence,
            "exchange": self.exchange,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OrderBookSnapshot':
        """Create snapshot from dictionary."""
        return cls.from_raw(
            symbol=data.get("symbol", "UNKNOWN"),
            bids=data.get("bids", []),
            asks=data.get("asks", []),
            timestamp=data.get("timestamp"),
            sequence=data.get("sequence", 0),
            exchange=data.get("exchange", ""),
        )

    def __repr__(self) -> str:
        if not self.is_valid:
            return f"OrderBook({self.symbol} @ {self.timestamp} | INVALID)"
        return (
            f"OrderBook({self.symbol} @ {self.timestamp} | "
            f"bid={self.best_bid:.4f} ask={self.best_ask:.4f} "
            f"spread={self.spread_bps:.2f}bps imb={self.imbalance:+.2f})"
        )


# ============================================================
# DATA VALIDATOR
# ============================================================

class DataValidator:
    """
    Validates order book data for anomalies.
    
    Detects:
    - Crossed order books (bid >= ask)
    - Extreme spreads
    - Price spikes (sudden large moves)
    - Stale data
    - Extreme imbalances
    
    Uses LRU-style cleanup to limit memory usage.
    """

    def __init__(
        self,
        max_spread_bps: float = OrderBookConstants.MAX_SPREAD_BPS_ALERT,
        max_price_spike_pct: float = OrderBookConstants.MAX_PRICE_SPIKE_PCT,
        history_size: int = 100,
        max_symbols: int = 500
    ):
        """
        Initialize validator.
        
        Args:
            max_spread_bps: Maximum acceptable spread in basis points
            max_price_spike_pct: Maximum acceptable price change percentage
            history_size: Price history size per symbol
            max_symbols: Maximum symbols to track (LRU eviction)
        """
        self.max_spread_bps = max_spread_bps
        self.max_price_spike_pct = max_price_spike_pct
        self._history_size = history_size
        self._max_symbols = max_symbols
        self._price_history: Dict[str, Deque[float]] = {}
        self._last_access: Dict[str, float] = {}
        self._lock = threading.RLock()

    def _cleanup_old_symbols(self) -> None:
        """Remove least recently used symbols to stay under limit."""
        if len(self._price_history) <= int(self._max_symbols * 0.9):
            return

        # Sort by last access time, keep most recent
        sorted_symbols = sorted(
            self._last_access.items(),
            key=lambda x: x[1],
            reverse=True
        )
        keep = {s for s, _ in sorted_symbols[:self._max_symbols - 1]}

        # Remove old symbols
        for symbol in set(self._price_history.keys()) - keep:
            self._price_history.pop(symbol, None)
            self._last_access.pop(symbol, None)

    def validate(self, snapshot: OrderBookSnapshot) -> Tuple[bool, List[str]]:
        """
        Validate a snapshot for anomalies.
        
        Args:
            snapshot: OrderBookSnapshot to validate
            
        Returns:
            Tuple of (is_valid, list_of_warnings)
            is_valid is False only for critical issues (crossed book, price spike)
        """
        warnings: List[str] = []
        is_critical = False

        # Basic validity check
        if not snapshot.is_valid:
            return False, ["Invalid snapshot (missing bids or asks)"]

        # Crossed book check
        if snapshot.is_crossed:
            return False, ["Crossed order book (bid >= ask)"]

        # Spread check
        if snapshot.spread_bps > self.max_spread_bps:
            warnings.append(f"Extreme spread: {snapshot.spread_bps:.2f} bps")

        # Staleness check
        if snapshot.is_stale():
            warnings.append(f"Stale data: {snapshot.age_ms()}ms old")

        # Price spike detection
        symbol = snapshot.symbol
        mid = snapshot.mid_price
        now = time.time()

        with self._lock:
            # Initialize history for new symbol
            if symbol not in self._price_history:
                if len(self._price_history) >= self._max_symbols:
                    self._cleanup_old_symbols()
                self._price_history[symbol] = deque(maxlen=self._history_size)

            self._last_access[symbol] = now
            history = self._price_history[symbol]

            # Check for price spike after warmup period
            if len(history) >= 10:
                avg_price = sum(history) / len(history)
                if avg_price > 0:
                    pct_change = abs(mid - avg_price) / avg_price * 100
                    if pct_change > self.max_price_spike_pct:
                        warnings.append(f"Price spike: {pct_change:.2f}% from avg")
                        is_critical = True

            history.append(mid)

        # Extreme imbalance check
        if abs(snapshot.imbalance) > 0.999:
            warnings.append(f"Extreme imbalance: {snapshot.imbalance:.4f}")

        return not is_critical, warnings

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear history for symbol or all symbols."""
        with self._lock:
            if symbol:
                self._price_history.pop(symbol, None)
                self._last_access.pop(symbol, None)
            else:
                self._price_history.clear()
                self._last_access.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get validator statistics."""
        with self._lock:
            return {
                "tracked_symbols": len(self._price_history),
                "max_symbols": self._max_symbols,
                "history_size": self._history_size,
            }


# ============================================================
# THROTTLED CALLBACK
# ============================================================

class ThrottledCallback:
    """
    Rate-limits callback invocations.
    
    Ensures callbacks are not called more frequently than specified interval.
    Stores the most recent pending data for potential flush.
    """

    __slots__ = (
        '_callback',
        '_min_interval_ms',
        '_last_call',
        '_lock',
        '_pending'
    )

    def __init__(
        self,
        callback: Callable[[Any], None],
        min_interval_ms: int = 100
    ):
        """
        Initialize throttled callback.
        
        Args:
            callback: Function to call with data
            min_interval_ms: Minimum interval between calls
        """
        self._callback = callback
        self._min_interval_ms = min_interval_ms
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._pending: Any = None

    def __call__(self, data: Any) -> None:
        """Process data, throttling if necessary."""
        now = time.time() * 1000
        should_call = False

        with self._lock:
            if now - self._last_call >= self._min_interval_ms:
                self._last_call = now
                self._pending = None
                should_call = True
            else:
                # Store for potential later flush
                self._pending = data

        if should_call:
            try:
                self._callback(data)
            except Exception as e:
                logger.error(f"Throttled callback error: {e}")

    def flush(self) -> None:
        """Invoke pending callback immediately if any."""
        pending = None
        with self._lock:
            pending = self._pending
            self._pending = None
            if pending is not None:
                self._last_call = time.time() * 1000

        if pending is not None:
            try:
                self._callback(pending)
            except Exception as e:
                logger.error(f"Throttled callback flush error: {e}")


# ============================================================
# ORDER BOOK DELTA HANDLER
# ============================================================

class OrderBookDeltaHandler:
    """
    Maintains order book state from incremental updates.
    
    Useful for exchanges that send deltas (changes) instead of
    full snapshots. Maintains internal state and rebuilds snapshots.
    """

    def __init__(self, symbol: str, max_levels: int = 100):
        """
        Initialize delta handler.
        
        Args:
            symbol: Trading pair symbol
            max_levels: Maximum levels to maintain per side
        """
        self.symbol = symbol
        self.max_levels = max_levels
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._last_update_id = 0
        self._lock = threading.RLock()

    def _build_snapshot(self) -> Optional[OrderBookSnapshot]:
        """Construct snapshot from current state."""
        if not self._bids or not self._asks:
            return None

        # Sort and limit levels
        sorted_bids = sorted(
            ((p, s) for p, s in self._bids.items() if s > 0),
            key=lambda x: -x[0]  # Descending by price
        )[:self.max_levels]

        sorted_asks = sorted(
            ((p, s) for p, s in self._asks.items() if s > 0),
            key=lambda x: x[0]  # Ascending by price
        )[:self.max_levels]

        return OrderBookSnapshot.from_raw(
            symbol=self.symbol,
            bids=sorted_bids,
            asks=sorted_asks,
            sequence=self._last_update_id
        )

    def apply_snapshot(
        self,
        bids: List[PriceSize],
        asks: List[PriceSize],
        update_id: int
    ) -> None:
        """
        Apply a full snapshot (replaces all data).
        
        Args:
            bids: List of [price, size] for bids
            asks: List of [price, size] for asks
            update_id: Exchange update ID
        """
        with self._lock:
            self._bids.clear()
            self._asks.clear()
            for price, size in bids:
                if size > 0:
                    self._bids[price] = size
            for price, size in asks:
                if size > 0:
                    self._asks[price] = size
            self._last_update_id = update_id

    def apply_delta(
        self,
        bids: List[PriceSize],
        asks: List[PriceSize],
        update_id: int
    ) -> Optional[OrderBookSnapshot]:
        """
        Apply incremental update and return new snapshot.
        
        Args:
            bids: Bid updates (size=0 means remove)
            asks: Ask updates (size=0 means remove)
            update_id: Exchange update ID
            
        Returns:
            New snapshot or None if update is stale
        """
        with self._lock:
            # Check for stale update
            if update_id <= self._last_update_id:
                return None

            # Apply bid updates
            for price, size in bids:
                if size == 0:
                    self._bids.pop(price, None)
                else:
                    self._bids[price] = size

            # Apply ask updates
            for price, size in asks:
                if size == 0:
                    self._asks.pop(price, None)
                else:
                    self._asks[price] = size

            self._last_update_id = update_id
            return self._build_snapshot()

    def get_snapshot(self) -> Optional[OrderBookSnapshot]:
        """Get current state as snapshot."""
        with self._lock:
            return self._build_snapshot()

    def reset(self) -> None:
        """Clear all state."""
        with self._lock:
            self._bids.clear()
            self._asks.clear()
            self._last_update_id = 0


# ============================================================
# ALERT MANAGER
# ============================================================

class AlertManager:
    """
    Centralized alert handling with throttling.
    
    Prevents alert floods by rate-limiting per throttle_key.
    Supports multiple handlers for flexible alert routing.
    """

    def __init__(
        self,
        throttle_seconds: int = OrderBookConstants.ALERT_THROTTLE_SECONDS,
        max_throttle_keys: int = OrderBookConstants.MAX_THROTTLE_KEYS
    ):
        """
        Initialize alert manager.
        
        Args:
            throttle_seconds: Minimum seconds between same-key alerts
            max_throttle_keys: Maximum throttle keys to track
        """
        self._handlers: List[Callable[[str, str, Dict[str, Any]], None]] = []
        self._throttle: Dict[str, float] = {}
        self._throttle_seconds = throttle_seconds
        self._max_throttle_keys = max_throttle_keys
        self._lock = threading.RLock()

    def add_handler(
        self,
        handler: Callable[[str, str, Dict[str, Any]], None]
    ) -> None:
        """Add an alert handler function."""
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def remove_handler(self, handler: Callable) -> None:
        """Remove an alert handler."""
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _cleanup_throttle(self, now: float) -> None:
        """Remove old throttle entries to prevent memory growth."""
        if len(self._throttle) > self._max_throttle_keys:
            cutoff = now - self._throttle_seconds * 2
            self._throttle = {
                k: v for k, v in self._throttle.items()
                if v > cutoff
            }

    def alert(
        self,
        level: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        throttle_key: Optional[str] = None
    ) -> bool:
        """
        Send an alert to all handlers.
        
        Args:
            level: Alert level (INFO, WARNING, CRITICAL)
            message: Alert message
            context: Additional context data
            throttle_key: Key for throttling (None = no throttling)
            
        Returns:
            True if alert was sent, False if throttled
        """
        # Check throttle
        if throttle_key:
            now = time.time()
            with self._lock:
                self._cleanup_throttle(now)
                if throttle_key in self._throttle:
                    if now - self._throttle[throttle_key] < self._throttle_seconds:
                        return False
                self._throttle[throttle_key] = now

        # Build context
        ctx = context.copy() if context else {}
        ctx["timestamp"] = datetime.now().isoformat()
        ctx["level"] = level

        # Get handlers
        with self._lock:
            handlers = list(self._handlers)

        # Invoke handlers
        for handler in handlers:
            try:
                handler(level, message, ctx)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")

        return True

    def critical(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        throttle_key: Optional[str] = None
    ) -> bool:
        """Send critical alert."""
        return self.alert(AlertLevel.CRITICAL.value, message, context, throttle_key)

    def warning(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        throttle_key: Optional[str] = None
    ) -> bool:
        """Send warning alert."""
        return self.alert(AlertLevel.WARNING.value, message, context, throttle_key)

    def info(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        throttle_key: Optional[str] = None
    ) -> bool:
        """Send info alert."""
        return self.alert(AlertLevel.INFO.value, message, context, throttle_key)

    def clear_throttle(self) -> None:
        """Clear all throttle entries."""
        with self._lock:
            self._throttle.clear()


def console_alert_handler(
    level: str,
    message: str,
    context: Dict[str, Any]
) -> None:
    """Default alert handler that logs to console with icons."""
    prefix = f"[{context.get('symbol', '')}] " if context.get('symbol') else ""
    icons = {
        "CRITICAL": "🚨",
        "WARNING": "⚠️",
        "INFO": "ℹ️"
    }
    log_fn = {
        "CRITICAL": logger.critical,
        "WARNING": logger.warning
    }.get(level, logger.info)
    log_fn(f"{icons.get(level, 'ℹ️')} {prefix}{message}")


# ============================================================
# STATE MANAGER
# ============================================================

class StateManager:
    """
    Persistent state management with crash recovery.
    
    Saves trading state to JSON for recovery after restarts.
    Uses atomic writes to prevent corruption.
    """

    def __init__(self, state_file: str = "trading_state.json"):
        """
        Initialize state manager.
        
        Args:
            state_file: Path to state file
        """
        self.state_file = Path(state_file)
        self._state: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._auto_save = True
        self._cleanup_temp_files()
        self._load()

    def _cleanup_temp_files(self) -> None:
        """Clean up orphaned temp files from previous runs."""
        try:
            state_dir = (
                self.state_file.parent
                if self.state_file.parent != Path()
                else Path(".")
            )
            if state_dir.exists():
                for tmp_file in state_dir.glob(f"{self.state_file.stem}.*.tmp"):
                    try:
                        tmp_file.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    def _load(self) -> None:
        """Load state from disk."""
        if not self.state_file.exists():
            return

        try:
            with open(self.state_file, 'r') as f:
                self._state = json.load(f)
            logger.info(f"Loaded state from {self.state_file}")
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted state file: {e}")
            self._backup_corrupted()
            self._state = {}
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            self._state = {}

    def _backup_corrupted(self) -> None:
        """Backup corrupted state file for analysis."""
        try:
            backup = self.state_file.with_suffix(f'.corrupted.{int(time.time())}')
            self.state_file.rename(backup)
            logger.info(f"Corrupted state backed up to {backup}")
        except Exception as e:
            logger.error(f"Failed to backup corrupted state: {e}")

    def save(self) -> bool:
        """
        Save state to disk atomically.
        
        Uses temp file + rename to prevent corruption.
        
        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            state_copy = self._state.copy()

        temp_file = self.state_file.with_suffix(f'.{os.getpid()}.tmp')
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_file, 'w') as f:
                json.dump(state_copy, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(self.state_file)
            return True
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from state."""
        with self._lock:
            return self._state.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        """Set a value in state."""
        with self._lock:
            self._state[key] = value
        if auto_save and self._auto_save:
            self.save()

    def delete(self, key: str, auto_save: bool = True) -> None:
        """Delete a key from state."""
        with self._lock:
            self._state.pop(key, None)
        if auto_save and self._auto_save:
            self.save()

    def update(self, data: Dict[str, Any], auto_save: bool = True) -> None:
        """Update multiple values in state."""
        with self._lock:
            self._state.update(data)
        if auto_save and self._auto_save:
            self.save()

    def get_all(self) -> Dict[str, Any]:
        """Get all state as a copy."""
        with self._lock:
            return self._state.copy()

    def clear(self, auto_save: bool = True) -> None:
        """Clear all state."""
        with self._lock:
            self._state.clear()
        if auto_save:
            self.save()

    def update_positions(self, positions: Dict[str, float]) -> None:
        """Update trading positions with timestamp."""
        self.update({
            "positions": positions,
            "positions_updated": datetime.now().isoformat(),
        })

    def get_positions(self) -> Dict[str, float]:
        """Get current trading positions."""
        return self.get("positions", {})

    def set_running(self, running: bool) -> None:
        """Mark bot as running/stopped."""
        self.set("running", running, auto_save=False)
        self.set("last_activity", datetime.now().isoformat())

    def was_running(self) -> bool:
        """Check if bot was running before crash."""
        return self.get("running", False)


# ============================================================
# ♦️ORDER BOOK ANALYZER
# ============================================================
class OrderBookAnalyzer:
    """
    Tracks order book evolution over time.
    
    Provides:
    - Trend analysis (imbalance trends, acceleration)
    - Signal generation (bullish/bearish detection)
    - Statistics computation with caching
    - Wall detection near prices
    """

    def __init__(
        self,
        max_snapshots: int = OrderBookConstants.MAX_SNAPSHOTS_DEFAULT
    ):
        """
        Initialize analyzer.
        
        Args:
            max_snapshots: Maximum snapshots to retain
        """
        self._snapshots: Deque[OrderBookSnapshot] = deque(maxlen=max_snapshots)
        self._lock = threading.RLock()
        self._last_sequence = 0
        self._sequence_gaps = 0
        self._total_updates = 0
        # Cache for statistics
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_key: Tuple[int, int] = (0, 0)

    def add_snapshot(self, snapshot: Optional[OrderBookSnapshot]) -> None:
        """
        Add a new snapshot to the analyzer.
        
        Tracks sequence gaps for data quality monitoring.
        """
        if snapshot is None:
            return

        with self._lock:
            # Track sequence gaps
            if snapshot.sequence > 0 and self._last_sequence > 0:
                if snapshot.sequence > self._last_sequence + 1:
                    gap = snapshot.sequence - self._last_sequence - 1
                    self._sequence_gaps += gap

            if snapshot.sequence > 0:
                self._last_sequence = snapshot.sequence

            self._snapshots.append(snapshot)
            self._total_updates += 1

            # Invalidate cache
            self._stats_cache = None

    def clear(self) -> None:
        """Clear all data and reset state."""
        with self._lock:
            self._snapshots.clear()
            self._last_sequence = 0
            self._sequence_gaps = 0
            self._total_updates = 0
            self._stats_cache = None

    @property
    def current(self) -> Optional[OrderBookSnapshot]:
        """Most recent snapshot."""
        with self._lock:
            return self._snapshots[-1] if self._snapshots else None

    @property
    def count(self) -> int:
        """Number of stored snapshots."""
        with self._lock:
            return len(self._snapshots)

    @property
    def is_empty(self) -> bool:
        """Check if analyzer has no data."""
        return self.count == 0

    @property
    def sequence_gaps(self) -> int:
        """Number of detected sequence gaps."""
        with self._lock:
            return self._sequence_gaps

    @property
    def total_updates(self) -> int:
        """Total updates processed."""
        with self._lock:
            return self._total_updates

    def is_data_fresh(
        self,
        max_age_ms: int = OrderBookConstants.STALE_DATA_MS
    ) -> bool:
        """Check if data is recent enough for trading."""
        current = self.current
        return current is not None and not current.is_stale(max_age_ms)

    def get_recent(self, n: int = 10) -> List[OrderBookSnapshot]:
        """
        Get the most recent n snapshots efficiently.
        
        Uses islice to avoid creating intermediate list copies.
        """
        with self._lock:
            length = len(self._snapshots)
            if length == 0:
                return []
            if length <= n:
                return list(self._snapshots)
            # Use islice for O(n) instead of O(2n) with list()[-n:]
            start = length - n
            return list(islice(self._snapshots, start, None))

    def get_snapshot_at(self, index: int) -> Optional[OrderBookSnapshot]:
        """Get snapshot at specific index."""
        with self._lock:
            try:
                return self._snapshots[index]
            except IndexError:
                return None

    def get_snapshot_at_time(
        self,
        timestamp_ms: int
    ) -> Optional[OrderBookSnapshot]:
        """
        Get snapshot at or before a specific time using binary search.
        
        Args:
            timestamp_ms: Target timestamp in milliseconds
            
        Returns:
            Snapshot at or before timestamp, or None if not found
        """
        with self._lock:
            if not self._snapshots:
                return None

            snapshots = self._snapshots
            left, right = 0, len(snapshots) - 1
            result: Optional[OrderBookSnapshot] = None

            while left <= right:
                mid = (left + right) // 2
                if snapshots[mid].timestamp <= timestamp_ms:
                    result = snapshots[mid]
                    left = mid + 1
                else:
                    right = mid - 1

            return result

    # ─────────────────────────────────────────────────────────
    # SPREAD ANALYSIS
    # ─────────────────────────────────────────────────────────

    def _get_valid_spreads(self, window: int) -> List[float]:
        """Get spread percentages from recent valid snapshots."""
        return [s.spread_pct for s in self.get_recent(window) if s.is_valid]

    def average_spread_pct(self, window: int = 20) -> float:
        """Average spread percentage over window."""
        spreads = self._get_valid_spreads(window)
        return sum(spreads) / len(spreads) if spreads else 0.0

    def average_spread_bps(self, window: int = 20) -> float:
        """Average spread in basis points over window."""
        return self.average_spread_pct(window) * 100

    def spread_volatility(self, window: int = 20) -> float:
        """Standard deviation of spread over window."""
        spreads = self._get_valid_spreads(window)
        if len(spreads) < 2:
            return 0.0
        mean = sum(spreads) / len(spreads)
        variance = sum((s - mean) ** 2 for s in spreads) / len(spreads)
        return variance ** 0.5

    def is_spread_acceptable(self, max_spread_pct: float = 0.1) -> bool:
        """Check if current spread is within threshold."""
        current = self.current
        return (
            current is not None
            and current.is_valid
            and current.spread_pct <= max_spread_pct
        )

    # ─────────────────────────────────────────────────────────
    # IMBALANCE ANALYSIS
    # ─────────────────────────────────────────────────────────

    def _get_valid_imbalances(self, window: int) -> List[float]:
        """Get imbalances from recent valid snapshots."""
        return [s.imbalance for s in self.get_recent(window) if s.is_valid]

    def average_imbalance(self, window: int = 20) -> float:
        """Average imbalance over window."""
        imbalances = self._get_valid_imbalances(window)
        return sum(imbalances) / len(imbalances) if imbalances else 0.0

    @staticmethod
    def _calc_slope(values: List[float]) -> float:
        """
        Calculate linear regression slope.
        
        Uses simplified formula for equally-spaced data points.
        """
        n = len(values)
        if n < 3:
            return 0.0

        sum_y = sum(values)
        sum_xy = sum(i * v for i, v in enumerate(values))
        n_f = float(n)

        # Denominator for equally-spaced x values (0, 1, 2, ...)
        denom = n_f * (n_f * n_f - 1) / 12
        if denom == 0:
            return 0.0

        return (sum_xy - (n_f - 1) / 2 * sum_y) / denom

    def imbalance_trend(self, window: int = 20) -> float:
        """
        Calculate imbalance trend (slope).
        
        Positive = becoming more bullish
        Negative = becoming more bearish
        """
        imbalances = self._get_valid_imbalances(window)
        return self._calc_slope(imbalances) if len(imbalances) >= 5 else 0.0

    def imbalance_acceleration(self, window: int = 20) -> float:
        """
        Calculate change in imbalance trend.
        
        Positive = trend accelerating bullish
        Negative = trend accelerating bearish
        """
        imbalances = self._get_valid_imbalances(window)
        if len(imbalances) < window:
            return 0.0

        half = window // 2
        if half < 3:
            return 0.0

        recent_slope = self._calc_slope(imbalances[half:])
        older_slope = self._calc_slope(imbalances[:half])
        return recent_slope - older_slope

    # ─────────────────────────────────────────────────────────
    # DIRECTIONAL SIGNALS
    # ─────────────────────────────────────────────────────────

    def _check_pressure(
        self,
        threshold: float,
        trend_threshold: float,
        require_both: bool,
        is_bullish: bool
    ) -> bool:
        """Check for directional pressure (bullish or bearish)."""
        current = self.current
        if not current or not current.is_valid:
            return False

        sign = 1 if is_bullish else -1
        imb_ok = current.imbalance * sign > threshold
        trend_ok = self.imbalance_trend() * sign > trend_threshold

        if require_both:
            return imb_ok and trend_ok
        return imb_ok or trend_ok

    def is_bullish(
        self,
        imbalance_threshold: float = 0.2,
        trend_threshold: float = 0.005,
        require_both: bool = False
    ) -> bool:
        """
        Check for bullish order book condition.
        
        Args:
            imbalance_threshold: Minimum imbalance for bullish signal
            trend_threshold: Minimum trend slope for bullish signal
            require_both: Require both conditions (default: either)
        """
        return self._check_pressure(
            imbalance_threshold,
            trend_threshold,
            require_both,
            is_bullish=True
        )

    def is_bearish(
        self,
        imbalance_threshold: float = 0.2,
        trend_threshold: float = 0.005,
        require_both: bool = False
    ) -> bool:
        """
        Check for bearish order book condition.
        
        Args:
            imbalance_threshold: Minimum imbalance magnitude for bearish
            trend_threshold: Minimum trend slope magnitude for bearish
            require_both: Require both conditions (default: either)
        """
        return self._check_pressure(
            imbalance_threshold,
            trend_threshold,
            require_both,
            is_bullish=False
        )

    def get_signal_strength(self) -> float:
        """
        Get composite signal strength.
        
        Combines:
        - Current imbalance (40%)
        - Depth imbalance (40%)
        - Imbalance trend (20%)
        
        Returns:
            -1.0 (strong bearish) to +1.0 (strong bullish)
        """
        current = self.current
        if not current or not current.is_valid:
            return 0.0

        signal = (
            current.imbalance * OrderBookConstants.SIGNAL_IMBALANCE_WEIGHT +
            current.depth_imbalance(10) * OrderBookConstants.SIGNAL_DEPTH_WEIGHT +
            self.imbalance_trend() * OrderBookConstants.SIGNAL_TREND_SCALE *
            OrderBookConstants.SIGNAL_TREND_WEIGHT
        )

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, signal))

    # ─────────────────────────────────────────────────────────
    # WALL DETECTION
    # ─────────────────────────────────────────────────────────

    def _has_wall_near(
        self,
        price: float,
        tolerance_pct: float,
        threshold_mult: float,
        wall_key: str
    ) -> bool:
        """Check for wall near price."""
        current = self.current
        if not current or not current.is_valid:
            return False

        walls = current.find_walls(threshold_mult).get(wall_key, [])
        if not walls:
            return False

        tolerance = price * (tolerance_pct / 100)
        return any(abs(w.price - price) <= tolerance for w in walls)

    def has_bid_wall_near(
        self,
        price: float,
        tolerance_pct: float = 0.5,
        threshold_mult: float = 3.0
    ) -> bool:
        """Check for bid wall (support) near price."""
        return self._has_wall_near(
            price,
            tolerance_pct,
            threshold_mult,
            "bid_walls"
        )

    def has_ask_wall_near(
        self,
        price: float,
        tolerance_pct: float = 0.5,
        threshold_mult: float = 3.0
    ) -> bool:
        """Check for ask wall (resistance) near price."""
        return self._has_wall_near(
            price,
            tolerance_pct,
            threshold_mult,
            "ask_walls"
        )

    # ─────────────────────────────────────────────────────────
    # STATISTICS
    # ─────────────────────────────────────────────────────────

    def get_statistics(self, window: int = 20) -> Dict[str, Any]:
        """
        Get comprehensive statistics.
        
        Returns cached result if no new data since last call.
        """
        # Check cache
        cache_key = (self._total_updates, window)
        if self._stats_cache is not None and self._stats_cache_key == cache_key:
            return self._stats_cache

        current = self.current
        if not current or not current.is_valid:
            return {}

        result = {
            # Current snapshot metrics
            "mid_price": current.mid_price,
            "spread": current.spread,
            "spread_pct": current.spread_pct,
            "spread_bps": current.spread_bps,
            "imbalance": current.imbalance,
            "depth_imbalance_10": current.depth_imbalance(10),

            # Time series metrics
            "avg_spread_pct": self.average_spread_pct(window),
            "avg_imbalance": self.average_imbalance(window),
            "imbalance_trend": self.imbalance_trend(window),

            # Signals
            "signal_strength": self.get_signal_strength(),

            # Advanced pricing
            "microprice": current.microprice(),
            "weighted_mid": current.weighted_mid_price(),

            # Volume
            "bid_volume_10": current.total_bid_volume(10),
            "ask_volume_10": current.total_ask_volume(10),

            # Health
            "snapshot_count": self.count,
            "sequence_gaps": self.sequence_gaps,
            "data_fresh": self.is_data_fresh(),
        }

        # Cache result
        self._stats_cache = result
        self._stats_cache_key = cache_key

        return result

# ============================================================
# ♦️TICK CLASSIFIER
# ============================================================

class TickClassifier:
    """
    Classifies ticks as buy or sell using tick rule.
    
    The tick rule:
    - Uptick (price > last) = BUY
    - Downtick (price < last) = SELL
    - No change = same as last classification
    
    Used for reconstructing order flow from trade data.
    """

    __slots__ = ('_last_price', '_last_side')

    def __init__(self):
        """Initialize classifier."""
        self._last_price: Optional[float] = None
        self._last_side = TradeSide.UNKNOWN

    def classify(self, price: float) -> TradeSide:
        """
        Classify a single tick.
        
        Args:
            price: Trade price
            
        Returns:
            TradeSide (BUY, SELL, or UNKNOWN)
        """
        if price <= 0:
            return TradeSide.UNKNOWN

        if self._last_price is None:
            self._last_price = price
            return TradeSide.UNKNOWN

        if price > self._last_price:
            side = TradeSide.BUY
        elif price < self._last_price:
            side = TradeSide.SELL
        else:
            # Same price - use last classification
            if self._last_side != TradeSide.UNKNOWN:
                side = self._last_side
            else:
                side = TradeSide.UNKNOWN

        self._last_price = price
        self._last_side = side
        return side

    def classify_batch(self, prices: List[float]) -> List[TradeSide]:
        """Classify multiple ticks in sequence."""
        return [self.classify(p) for p in prices]

    def reset(self) -> None:
        """Reset classifier state."""
        self._last_price = None
        self._last_side = TradeSide.UNKNOWN


# ============================================================
# ORDER FLOW METRICS & TRACKER
# ============================================================

@dataclass
class OrderFlowMetrics:
    """
    Order flow analysis metrics for strategy signals.
    
    Tracks cumulative delta (buy vs sell pressure) and
    large trade activity for institutional flow detection.
    
    Attributes:
        cumulative_delta: Running sum of signed volume (+ for buys, - for sells)
        buy_volume: Total volume classified as buys
        sell_volume: Total volume classified as sells
        large_buy_count: Number of large buy trades
        large_sell_count: Number of large sell trades
        trade_count: Number of successfully classified trades
        unknown_count: Number of trades that couldn't be classified
    """
    cumulative_delta: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    large_buy_count: int = 0
    large_sell_count: int = 0
    trade_count: int = 0
    unknown_count: int = 0

    @property
    def ofi(self) -> float:
        """
        Order Flow Imbalance: normalized buying vs selling pressure.
        
        Returns:
            -1.0 (all sells) to +1.0 (all buys)
        """
        total = self.buy_volume + self.sell_volume
        if total == 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    @property
    def total_volume(self) -> float:
        """Total classified traded volume."""
        return self.buy_volume + self.sell_volume

    @property
    def total_trade_count(self) -> int:
        """Total trades including unclassified."""
        return self.trade_count + self.unknown_count

    @property
    def classification_rate(self) -> float:
        """Percentage of trades successfully classified."""
        total = self.total_trade_count
        if total == 0:
            return 0.0
        return self.trade_count / total

    @property
    def large_trade_imbalance(self) -> float:
        """
        Imbalance in large trades (institutional activity indicator).
        
        Returns:
            -1.0 (all large sells) to +1.0 (all large buys)
        """
        total = self.large_buy_count + self.large_sell_count
        if total == 0:
            return 0.0
        return (self.large_buy_count - self.large_sell_count) / total

    def to_dict(self) -> Dict[str, Any]:
        """Export as dictionary."""
        return {
            "cumulative_delta": round(self.cumulative_delta, 6),
            "buy_volume": round(self.buy_volume, 6),
            "sell_volume": round(self.sell_volume, 6),
            "ofi": round(self.ofi, 4),
            "total_volume": round(self.total_volume, 6),
            "large_buy_count": self.large_buy_count,
            "large_sell_count": self.large_sell_count,
            "large_trade_imbalance": round(self.large_trade_imbalance, 4),
            "trade_count": self.trade_count,
            "unknown_count": self.unknown_count,
            "classification_rate": round(self.classification_rate, 4),
        }


class OrderFlowTracker:
    """
    Tracks order flow for strategy signals.
    
    Uses tick rule to classify trades as buys/sells and tracks:
    - Cumulative delta (running sum of signed volume)
    - Large trade detection (institutional activity)
    - Order flow imbalance (OFI)
    - Recent delta momentum
    
    Essential for:
    - Detecting hidden buying/selling pressure
    - Identifying institutional accumulation/distribution
    - Confirming breakouts with volume
    """

    def __init__(
        self,
        large_trade_threshold: float = OrderBookConstants.LARGE_TRADE_THRESHOLD,
        window_size: int = 100
    ):
        """
        Initialize order flow tracker.
        
        Args:
            large_trade_threshold: Size threshold for "large" trades
            window_size: Window for recent delta calculations
        """
        self.large_threshold = large_trade_threshold
        self.window_size = window_size
        self._classifier = TickClassifier()
        self._metrics = OrderFlowMetrics()
        self._recent_deltas: Deque[float] = deque(maxlen=window_size)
        self._lock = threading.Lock()

    def process_trade(self, price: float, size: float) -> OrderFlowMetrics:
        """
        Process a single trade and update metrics.
        
        Args:
            price: Trade price
            size: Trade size
            
        Returns:
            Current OrderFlowMetrics snapshot
        """
        if price <= 0 or size <= 0:
            return self.get_metrics()

        side = self._classifier.classify(price)

        with self._lock:
            if side == TradeSide.BUY:
                self._metrics.trade_count += 1
                self._metrics.buy_volume += size
                self._metrics.cumulative_delta += size
                self._recent_deltas.append(size)
                if size >= self.large_threshold:
                    self._metrics.large_buy_count += 1

            elif side == TradeSide.SELL:
                self._metrics.trade_count += 1
                self._metrics.sell_volume += size
                self._metrics.cumulative_delta -= size
                self._recent_deltas.append(-size)
                if size >= self.large_threshold:
                    self._metrics.large_sell_count += 1

            else:
                # Unknown classification
                self._metrics.unknown_count += 1
                self._recent_deltas.append(0)

            return self._copy_metrics()

    def process_batch(self, trades: List[SimpleTrade]) -> OrderFlowMetrics:
        """
        Process multiple trades: [(price, size), ...]
        
        Args:
            trades: List of (price, size) tuples
            
        Returns:
            Current OrderFlowMetrics snapshot
        """
        for price, size in trades:
            self.process_trade(price, size)
        return self.get_metrics()

    def _copy_metrics(self) -> OrderFlowMetrics:
        """Create a copy of current metrics (called within lock)."""
        return OrderFlowMetrics(
            cumulative_delta=self._metrics.cumulative_delta,
            buy_volume=self._metrics.buy_volume,
            sell_volume=self._metrics.sell_volume,
            large_buy_count=self._metrics.large_buy_count,
            large_sell_count=self._metrics.large_sell_count,
            trade_count=self._metrics.trade_count,
            unknown_count=self._metrics.unknown_count,
        )

    def get_metrics(self) -> OrderFlowMetrics:
        """Get current metrics snapshot."""
        with self._lock:
            return self._copy_metrics()

    def get_recent_delta(self) -> float:
        """Get sum of recent deltas (windowed cumulative delta)."""
        with self._lock:
            return sum(self._recent_deltas)

    def get_delta_momentum(self) -> float:
        """
        Get delta momentum (recent vs older activity).
        
        Positive = accelerating buying
        Negative = accelerating selling
        """
        with self._lock:
            if len(self._recent_deltas) < 10:
                return 0.0

            deltas = list(self._recent_deltas)
            half = len(deltas) // 2
            recent = sum(deltas[half:])
            older = sum(deltas[:half])
            return recent - older

    def reset(self) -> None:
        """Reset all tracking state."""
        with self._lock:
            self._metrics = OrderFlowMetrics()
            self._recent_deltas.clear()
            self._classifier.reset()


# ============================================================
#🎀 SLIPPAGE ESTIMATOR
# ============================================================

class SlippageEstimator:
    """
    Estimates slippage for trade execution simulation.
    
    Provides multiple estimation methods:
    - Realistic: Order book-based simulation
    - Fixed: Constant basis points slippage
    - Fill simulation with slippage limits
    
    Critical for realistic backtesting PnL calculation.
    """

    @staticmethod
    def estimate(
        snapshot: OrderBookSnapshot,
        size: float,
        side: str,
        aggression: float = 1.0
    ) -> Tuple[float, float, float]:
        """
        Estimate execution price and slippage.
        
        Args:
            snapshot: Current order book
            size: Order size
            side: 'buy' or 'sell'
            aggression: 0.0 (passive limit) to 1.0 (aggressive market)
        
        Returns:
            Tuple of (estimated_price, slippage_bps, fill_probability)
        """
        if not snapshot or not snapshot.is_valid:
            return 0.0, 0.0, 0.0

        mid = snapshot.mid_price

        # Passive order: sits at best bid/ask
        if aggression < 0.3:
            if side == "buy":
                price = snapshot.best_bid
            else:
                price = snapshot.best_ask

            # Calculate slippage from mid
            if mid > 0:
                slippage = abs(price - mid) / mid * 10000
            else:
                slippage = 0.0

            # Lower fill probability for passive orders
            fill_prob = 0.3 + aggression

        # Aggressive order: crosses spread + walks book
        else:
            price = snapshot.market_impact(size, side)

            # Calculate slippage from mid
            if mid > 0:
                slippage = abs(price - mid) / mid * 10000
            else:
                slippage = 0.0

            fill_prob = min(1.0, 0.7 + aggression * 0.3)

        return price, slippage, fill_prob

    @staticmethod
    def estimate_fixed(
        price: float,
        side: str,
        slippage_bps: float = OrderBookConstants.DEFAULT_SLIPPAGE_BPS
    ) -> float:
        """
        Apply fixed slippage to a price.
        
        Args:
            price: Base price
            side: 'buy' or 'sell'
            slippage_bps: Slippage in basis points
            
        Returns:
            Adjusted price after slippage
        """
        slip_mult = slippage_bps / 10000

        if side == "buy":
            return price * (1 + slip_mult)  # Pay more to buy
        else:
            return price * (1 - slip_mult)  # Receive less to sell

    @staticmethod
    def simulate_fill(
        snapshot: OrderBookSnapshot,
        size: float,
        side: str,
        max_slippage_bps: float = 10.0
    ) -> Optional[Tuple[float, float]]:
        """
        Simulate order fill with slippage limit.
        
        Args:
            snapshot: Order book snapshot
            size: Order size
            side: 'buy' or 'sell'
            max_slippage_bps: Maximum acceptable slippage
        
        Returns:
            (fill_price, filled_size) or None if unfillable within limit
        """
        if not snapshot or not snapshot.is_valid:
            return None

        mid = snapshot.mid_price
        if mid <= 0:
            return None

        # Calculate price limit based on slippage
        if side == "buy":
            max_price = mid * (1 + max_slippage_bps / 10000)
            levels = snapshot.asks
        else:
            max_price = mid * (1 - max_slippage_bps / 10000)
            levels = snapshot.bids

        # Walk through levels
        filled = 0.0
        cost = 0.0

        for level in levels:
            # Check price limit
            if side == "buy" and level.price > max_price:
                break
            if side == "sell" and level.price < max_price:
                break

            fill_at_level = min(size - filled, level.size)
            filled += fill_at_level
            cost += fill_at_level * level.price

            if filled >= size:
                break

        if filled == 0:
            return None

        return cost / filled, filled

    @staticmethod
    def calculate_market_impact_cost(
        snapshot: OrderBookSnapshot,
        size: float,
        side: str
    ) -> Dict[str, float]:
        """
        Calculate detailed market impact costs.
        
        Returns breakdown of execution costs for analysis.
        """
        if not snapshot or not snapshot.is_valid:
            return {"error": "invalid_snapshot"}

        mid = snapshot.mid_price
        fill_price = snapshot.market_impact(size, side)
        half_spread = snapshot.spread / 2

        # Estimate the price if we just crossed the spread
        if side == "buy":
            cross_price = mid + half_spread
        else:
            cross_price = mid - half_spread

        # Price impact beyond crossing spread
        price_impact = abs(fill_price - cross_price)

        return {
            "mid_price": mid,
            "fill_price": fill_price,
            "half_spread_cost": half_spread,
            "price_impact": price_impact,
            "total_slippage": abs(fill_price - mid),
            "slippage_bps": abs(fill_price - mid) / mid * 10000 if mid > 0 else 0,
            "total_cost_pct": abs(fill_price - mid) / mid * 100 if mid > 0 else 0,
        }


# ============================================================
# BACKTEST EXECUTION SIMULATOR
# ============================================================

class BacktestExecutionSimulator:
    """
    Simulates trade execution for backtesting.
    
    Integrates with order book data to provide realistic execution
    simulation including:
    - Slippage modeling (none, fixed, or realistic)
    - Fee calculation
    - Partial fills (optional)
    - Complete round-trip simulation
    - Execution statistics tracking
    """

    def __init__(
        self,
        slippage_model: str = "realistic",
        fixed_slippage_bps: float = OrderBookConstants.DEFAULT_SLIPPAGE_BPS,
        fee_bps: float = OrderBookConstants.DEFAULT_FEE_BPS,
        partial_fill_enabled: bool = False
    ):
        """
        Initialize execution simulator.
        
        Args:
            slippage_model: "none", "fixed", or "realistic"
            fixed_slippage_bps: Slippage for fixed model (default 2 bps)
            fee_bps: Trading fee in basis points (default 4 bps = 0.04%)
            partial_fill_enabled: Whether to simulate partial fills
        """
        if slippage_model not in ("none", "fixed", "realistic"):
            raise ValueError(
                f"Invalid slippage_model: {slippage_model}. "
                f"Must be 'none', 'fixed', or 'realistic'"
            )

        self.slippage_model = slippage_model
        self.fixed_slippage_bps = fixed_slippage_bps
        self.fee_bps = fee_bps
        self.partial_fill_enabled = partial_fill_enabled

        # Statistics tracking
        self._total_fees = 0.0
        self._total_slippage_cost = 0.0
        self._trade_count = 0

    def simulate_entry(
        self,
        signal_price: float,
        size: float,
        side: str,
        orderbook: Optional[OrderBookSnapshot] = None,
        aggression: float = 1.0
    ) -> Dict[str, Any]:
        """
        Simulate order entry.
        
        Args:
            signal_price: Price at which signal was generated
            size: Order size
            side: 'buy' or 'sell'
            orderbook: Optional order book for realistic slippage
            aggression: 0.0 (passive) to 1.0 (aggressive)
        
        Returns:
            Execution details dictionary
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {side}. Must be 'buy' or 'sell'")

        if size <= 0:
            raise ValueError(f"Size must be positive: {size}")

        # Determine fill price based on slippage model
        if self.slippage_model == "none":
            fill_price = signal_price
            slippage_bps = 0.0
            fill_size = size
            fill_prob = 1.0

        elif self.slippage_model == "fixed":
            fill_price = SlippageEstimator.estimate_fixed(
                signal_price,
                side,
                self.fixed_slippage_bps
            )
            slippage_bps = self.fixed_slippage_bps
            fill_size = size
            fill_prob = 1.0

        else:  # realistic
            if orderbook and orderbook.is_valid:
                fill_price, slippage_bps, fill_prob = SlippageEstimator.estimate(
                    orderbook,
                    size,
                    side,
                    aggression
                )

                # Handle partial fills
                if self.partial_fill_enabled and fill_prob < 1.0:
                    fill_size = size * fill_prob
                else:
                    fill_size = size
            else:
                # Fallback to fixed model if no orderbook
                fill_price = SlippageEstimator.estimate_fixed(
                    signal_price,
                    side,
                    self.fixed_slippage_bps
                )
                slippage_bps = self.fixed_slippage_bps
                fill_size = size
                fill_prob = 1.0

        # Calculate fee
        fee = fill_price * fill_size * (self.fee_bps / 10000)

        # Calculate total outlay/proceeds
        if side == "buy":
            total_outlay = fill_price * fill_size + fee
            net_proceeds = 0.0
        else:
            total_outlay = 0.0
            net_proceeds = fill_price * fill_size - fee

        # Track statistics
        self._total_fees += fee
        slippage_cost = abs(fill_price - signal_price) * fill_size
        self._total_slippage_cost += slippage_cost
        self._trade_count += 1

        return {
            "signal_price": signal_price,
            "fill_price": fill_price,
            "size": fill_size,
            "requested_size": size,
            "side": side,
            "slippage_bps": slippage_bps,
            "slippage_cost": slippage_cost,
            "fee": fee,
            "fee_bps": self.fee_bps,
            "total_outlay": total_outlay,
            "net_proceeds": net_proceeds,
            "fill_probability": fill_prob,
            "fully_filled": fill_size >= size * 0.999,
            "timestamp": _now_ms(),
        }

    def simulate_exit(
        self,
        entry: Dict[str, Any],
        exit_price: float,
        orderbook: Optional[OrderBookSnapshot] = None,
        aggression: float = 1.0
    ) -> Dict[str, Any]:
        """
        Simulate exit and calculate PnL.
        
        Args:
            entry: Entry execution dict from simulate_entry()
            exit_price: Signal exit price
            orderbook: Optional order book for realistic slippage
            aggression: 0.0 (passive) to 1.0 (aggressive)
        
        Returns:
            Complete trade result with PnL
        """
        # Determine exit side
        exit_side = "sell" if entry["side"] == "buy" else "buy"

        # Simulate exit
        exit_result = self.simulate_entry(
            exit_price,
            entry["size"],
            exit_side,
            orderbook,
            aggression
        )

        # Calculate PnL
        if entry["side"] == "buy":
            # Long trade: profit = exit - entry
            gross_pnl = (
                exit_result["fill_price"] - entry["fill_price"]
            ) * entry["size"]
        else:
            # Short trade: profit = entry - exit
            gross_pnl = (
                entry["fill_price"] - exit_result["fill_price"]
            ) * entry["size"]

        # Total costs
        total_fees = entry["fee"] + exit_result["fee"]
        total_slippage_cost = entry["slippage_cost"] + exit_result["slippage_cost"]
        net_pnl = gross_pnl - total_fees

        # Calculate return percentage
        if entry["side"] == "buy":
            initial_value = entry["fill_price"] * entry["size"]
        else:
            initial_value = entry["fill_price"] * entry["size"]

        return_pct = (net_pnl / initial_value * 100) if initial_value > 0 else 0.0

        return {
            "entry": entry,
            "exit": exit_result,
            "gross_pnl": gross_pnl,
            "fees": total_fees,
            "slippage_cost": total_slippage_cost,
            "net_pnl": net_pnl,
            "return_pct": return_pct,
            "hold_time_ms": exit_result["timestamp"] - entry["timestamp"],
            "profitable": net_pnl > 0,
        }

    def simulate_round_trip(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        side: str,
        entry_orderbook: Optional[OrderBookSnapshot] = None,
        exit_orderbook: Optional[OrderBookSnapshot] = None,
        aggression: float = 1.0
    ) -> Dict[str, Any]:
        """
        Simulate complete round-trip trade.
        
        Convenience method for simulating entry and exit together.
        
        Args:
            entry_price: Signal entry price
            exit_price: Signal exit price
            size: Trade size
            side: Entry side ('buy' or 'sell')
            entry_orderbook: Order book at entry
            exit_orderbook: Order book at exit
            aggression: Execution aggression level
            
        Returns:
            Complete trade result
        """
        entry = self.simulate_entry(
            entry_price,
            size,
            side,
            entry_orderbook,
            aggression
        )
        return self.simulate_exit(entry, exit_price, exit_orderbook, aggression)

    def get_statistics(self) -> Dict[str, Any]:
        """Get execution statistics."""
        return {
            "trade_count": self._trade_count,
            "total_fees": round(self._total_fees, 6),
            "total_slippage_cost": round(self._total_slippage_cost, 6),
            "avg_fee_per_trade": round(
                self._total_fees / max(1, self._trade_count), 6
            ),
            "avg_slippage_per_trade": round(
                self._total_slippage_cost / max(1, self._trade_count), 6
            ),
            "slippage_model": self.slippage_model,
            "fee_bps": self.fee_bps,
        }

    def reset_statistics(self) -> None:
        """Reset execution statistics."""
        self._total_fees = 0.0
        self._total_slippage_cost = 0.0
        self._trade_count = 0


# ============================================================
# SYNTHETIC ORDER BOOK
# ============================================================

class SyntheticOrderBook:
    """
    Generates synthetic order books from tick data.
    
    Essential for backtesting when order book data is unavailable.
    
    The generated order book uses:
    - Recent price volatility to determine spread
    - Tick classification to estimate imbalance
    - Size data (if available) to weight levels
    - Reproducible randomness for consistent backtests
    """

    @staticmethod
    def from_ticks(
        ticks: List[TickData],
        tick_sizes: Optional[List[float]] = None,
        spread_bps: float = OrderBookConstants.DEFAULT_SPREAD_BPS,
        levels: int = OrderBookConstants.DEFAULT_LEVELS,
        size_decay: float = OrderBookConstants.DEFAULT_SIZE_DECAY,
        base_size: float = OrderBookConstants.DEFAULT_BASE_SIZE,
        use_tick_classification: bool = True,
        random_seed: Optional[int] = None
    ) -> Optional[OrderBookSnapshot]:
        """
        Generate a synthetic order book from tick data.
        
        Args:
            ticks: List of (timestamp_ms, price) tuples
            tick_sizes: Optional list of trade sizes (parallel to ticks)
            spread_bps: Base spread in basis points
            levels: Number of price levels to generate
            size_decay: Size decay factor per level (0-1)
            base_size: Base size for level 0
            use_tick_classification: Use tick rule for imbalance estimation
            random_seed: For reproducible results
            
        Returns:
            OrderBookSnapshot or None if insufficient data
        """
        if not ticks:
            return None

        # Use isolated RNG for reproducibility
        rng = random.Random(random_seed)

        # Get latest tick
        last_ts, last_price = ticks[-1]
        if last_price <= 0:
            return None

        # Calculate spread based on recent volatility
        spread_pct = spread_bps / 10000

        if len(ticks) > 10:
            recent_prices = [p for _, p in ticks[-20:]]
            min_p = min(recent_prices)
            max_p = max(recent_prices)
            avg_p = sum(recent_prices) / len(recent_prices)

            if avg_p > 0:
                vol_ratio = (max_p - min_p) / avg_p
                # Increase spread based on volatility
                spread_mult = max(
                    1.0,
                    1 + vol_ratio * OrderBookConstants.VOLATILITY_SPREAD_MULT
                )
                spread_mult = min(spread_mult, OrderBookConstants.MAX_SPREAD_MULT)
                spread_pct *= spread_mult

        half_spread = last_price * spread_pct / 2
        tick_size = last_price * 0.0001  # 1 bps tick size

        # Calculate imbalance from tick classification
        imbalance_mult = 1.0
        if use_tick_classification and len(ticks) > 5:
            classifier = TickClassifier()
            recent_ticks = ticks[-50:] if len(ticks) >= 50 else ticks
            sides = classifier.classify_batch([p for _, p in recent_ticks])
            buys = sum(1 for s in sides if s == TradeSide.BUY)
            sells = sum(1 for s in sides if s == TradeSide.SELL)

            if buys + sells > 0:
                # Adjust imbalance multiplier based on flow
                imbalance_mult = 1 + (buys - sells) / (buys + sells) * 0.3

        # Apply size weighting if available
        size_weight = 1.0
        if tick_sizes and len(tick_sizes) >= len(ticks):
            recent_sizes = tick_sizes[-20:]
            if recent_sizes and max(recent_sizes) > 0:
                avg_size = sum(recent_sizes) / len(recent_sizes)
                size_weight = max(0.5, min(2.0, avg_size / base_size))

        # Generate levels
        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []

        for i in range(levels):
            # Size decays with distance from mid
            size = base_size * size_weight * (size_decay ** i)

            # Add randomness for realism
            jitter_bid = 0.5 + rng.random()
            jitter_ask = 0.5 + rng.random()

            # Calculate prices
            bid_price = last_price - half_spread - tick_size * i
            ask_price = last_price + half_spread + tick_size * i

            # Apply imbalance (more bids if bullish, more asks if bearish)
            bid_size = size * imbalance_mult * jitter_bid
            ask_size = size * (2 - imbalance_mult) * jitter_ask

            bids.append(OrderBookLevel(bid_price, bid_size))
            asks.append(OrderBookLevel(ask_price, ask_size))

        return OrderBookSnapshot.from_sorted(
            symbol="SYNTHETIC",
            timestamp=last_ts,
            bids=bids,
            asks=asks,
            exchange="synthetic"
        )

    @staticmethod
    def from_candle(
        timestamp: int,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float,
        levels: int = OrderBookConstants.DEFAULT_LEVELS,
        random_seed: Optional[int] = None
    ) -> OrderBookSnapshot:
        """
        Generate synthetic order book from OHLCV candle.
        
        Uses candle data to estimate spread and size distribution.
        
        Args:
            timestamp: Candle timestamp
            open_price: Open price
            high_price: High price
            low_price: Low price
            close_price: Close price (used as current price)
            volume: Candle volume
            levels: Number of price levels
            random_seed: For reproducibility
            
        Returns:
            OrderBookSnapshot
            
        Raises:
            ValueError: If close_price is not positive
        """
        if close_price <= 0:
            raise ValueError(f"Close price must be positive: {close_price}")

        rng = random.Random(random_seed)

        # Spread based on volume (more volume = tighter spread)
        if volume > 0:
            spread_bps = max(
                OrderBookConstants.MIN_SPREAD_BPS,
                10.0 / (1 + volume / 1000)
            )
        else:
            spread_bps = OrderBookConstants.DEFAULT_SPREAD_BPS

        half_spread = close_price * (spread_bps / 10000 / 2)
        tick_size = close_price * 0.0001
        base_size = max(1.0, volume / levels / 20)

        # Generate levels
        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []

        for i in range(levels):
            size = base_size * (OrderBookConstants.DEFAULT_SIZE_DECAY ** i)
            jitter = 0.5 + rng.random()

            bid_price = close_price - half_spread - tick_size * i
            ask_price = close_price + half_spread + tick_size * i

            bids.append(OrderBookLevel(bid_price, size * jitter))
            asks.append(OrderBookLevel(ask_price, size * jitter))

        return OrderBookSnapshot.from_sorted(
            symbol="SYNTHETIC",
            timestamp=timestamp,
            bids=bids,
            asks=asks,
            exchange="synthetic"
        )

    @staticmethod
    def generate_analyzer(
        ticks: List[TickData],
        tick_sizes: Optional[List[float]] = None,
        sample_every: int = 10,
        **kwargs
    ) -> Optional[OrderBookAnalyzer]:
        """
        Generate an OrderBookAnalyzer with historical snapshots.
        
        Creates snapshots at regular intervals from tick data.
        Useful for backtesting strategies that need order book history.
        
        Args:
            ticks: List of (timestamp_ms, price) tuples
            tick_sizes: Optional list of trade sizes
            sample_every: Generate snapshot every N ticks
            **kwargs: Passed to from_ticks()
            
        Returns:
            OrderBookAnalyzer with historical snapshots, or None if insufficient data
        """
        if not ticks or len(ticks) < sample_every:
            return None

        analyzer = OrderBookAnalyzer()

        for i in range(sample_every, len(ticks) + 1, sample_every):
            snapshot = SyntheticOrderBook.from_ticks(
                ticks=ticks[:i],
                tick_sizes=tick_sizes[:i] if tick_sizes else None,
                random_seed=i,  # Reproducible per snapshot
                **kwargs
            )
            if snapshot and snapshot.is_valid:
                analyzer.add_snapshot(snapshot)

        return analyzer if analyzer.count > 0 else None


# ============================================================
# BACKTEST ORDER BOOK PROVIDER
# ============================================================

class BacktestOrderBookProvider:
    """
    Provides order book data for backtesting.
    
    Supports loading from:
    - Pre-recorded snapshots
    - Tick data (generates synthetic order books)
    - Candle data (generates synthetic order books)
    
    Provides time-based lookup for historical replay.
    """

    def __init__(self):
        """Initialize backtest provider."""
        self._analyzers: Dict[str, OrderBookAnalyzer] = {}
        self._current_time = 0
        self._lock = threading.RLock()

    def load_snapshots(
        self,
        symbol: str,
        snapshots: List[OrderBookSnapshot]
    ) -> None:
        """
        Load pre-recorded snapshots.
        
        Args:
            symbol: Trading pair symbol
            snapshots: List of snapshots (will be sorted by timestamp)
        """
        with self._lock:
            if symbol not in self._analyzers:
                self._analyzers[symbol] = OrderBookAnalyzer()

            # Sort and add snapshots
            for snap in sorted(snapshots, key=lambda s: s.timestamp):
                self._analyzers[symbol].add_snapshot(snap)

    def load_from_ticks(
        self,
        symbol: str,
        ticks: List[TickData],
        tick_sizes: Optional[List[float]] = None,
        sample_every: int = 10,
        **kwargs
    ) -> None:
        """
        Generate synthetic order book from tick data.
        
        Args:
            symbol: Trading pair symbol
            ticks: List of (timestamp_ms, price) tuples
            tick_sizes: Optional list of trade sizes
            sample_every: Generate snapshot every N ticks
            **kwargs: Additional arguments for SyntheticOrderBook
        """
        analyzer = SyntheticOrderBook.generate_analyzer(
            ticks,
            tick_sizes,
            sample_every,
            **kwargs
        )
        if analyzer:
            with self._lock:
                self._analyzers[symbol] = analyzer

    def load_from_candles(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        **kwargs
    ) -> None:
        """
        Generate synthetic order book from OHLCV candles.
        
        Args:
            symbol: Trading pair symbol
            candles: List of candle dicts with keys:
                     timestamp, open, high, low, close, volume
            **kwargs: Additional arguments for SyntheticOrderBook
        """
        with self._lock:
            if symbol not in self._analyzers:
                self._analyzers[symbol] = OrderBookAnalyzer()

            for i, candle in enumerate(candles):
                try:
                    snapshot = SyntheticOrderBook.from_candle(
                        timestamp=candle.get('timestamp', i * 60000),
                        open_price=candle.get('open', 0),
                        high_price=candle.get('high', 0),
                        low_price=candle.get('low', 0),
                        close_price=candle.get('close', 0),
                        volume=candle.get('volume', 0),
                        random_seed=i,
                        **kwargs
                    )
                    self._analyzers[symbol].add_snapshot(snapshot)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Failed to create snapshot from candle {i}: {e}")

    def set_time(self, timestamp_ms: int) -> None:
        """
        Set current backtest time.
        
        Args:
            timestamp_ms: Current time in milliseconds
        """
        self._current_time = timestamp_ms

    def get_analyzer(self, symbol: str) -> Optional[OrderBookAnalyzer]:
        """Get analyzer for symbol."""
        with self._lock:
            return self._analyzers.get(symbol)

    def get_snapshot(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """Get latest snapshot for symbol."""
        analyzer = self.get_analyzer(symbol)
        return analyzer.current if analyzer else None

    def get_snapshot_at_time(
        self,
        symbol: str,
        timestamp_ms: int
    ) -> Optional[OrderBookSnapshot]:
        """
        Get snapshot at or before a specific time.
        
        Uses binary search for efficient lookup.
        
        Args:
            symbol: Trading pair symbol
            timestamp_ms: Target timestamp
            
        Returns:
            Snapshot at or before timestamp, or None
        """
        with self._lock:
            analyzer = self._analyzers.get(symbol)
            if not analyzer:
                return None
            return analyzer.get_snapshot_at_time(timestamp_ms)

    def get_all_symbols(self) -> List[str]:
        """Get all loaded symbols."""
        with self._lock:
            return list(self._analyzers.keys())

    def clear(self, symbol: Optional[str] = None) -> None:
        """
        Clear data for symbol or all symbols.
        
        Args:
            symbol: Symbol to clear, or None for all
        """
        with self._lock:
            if symbol:
                self._analyzers.pop(symbol, None)
            else:
                self._analyzers.clear()


# ============================================================
# BASE STREAM INTERFACE
# ============================================================

class BaseOrderBookStream(ABC):
    """Abstract base class for order book streams."""

    @abstractmethod
    def subscribe(self, symbol: str, callback: Optional[Callback] = None) -> None:
        """Subscribe to order book updates for a symbol."""
        pass

    @abstractmethod
    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        pass

    @abstractmethod
    def get_analyzer(self, symbol: str) -> Optional[OrderBookAnalyzer]:
        """Get analyzer for symbol."""
        pass

    @abstractmethod
    def get_snapshot(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """Get latest snapshot for symbol."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the stream."""
        pass

    def get_health(self, symbol: str) -> Optional[StreamHealth]:
        """Get health metrics for symbol."""
        return None

    def get_all_health(self) -> Dict[str, StreamHealth]:
        """Get health metrics for all symbols."""
        return {}


# ============================================================
# STREAM BASE CLASS
# ============================================================

class _StreamBase(BaseOrderBookStream):
    """
    Common implementation for WebSocket streams.
    
    Provides:
    - Symbol tracking with analyzers
    - Callback management
    - Health monitoring
    - Background thread management
    - Graceful shutdown
    """

    def __init__(self):
        """Initialize stream base."""
        self._analyzers: Dict[str, OrderBookAnalyzer] = {}
        self._callbacks: Dict[str, List[Callback]] = {}
        self._health: Dict[str, StreamHealth] = {}
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._init_lock = threading.RLock()
        self._started_event = threading.Event()

    @staticmethod
    def _validate_symbol(symbol: str) -> bool:
        """Validate symbol format."""
        return bool(OrderBookConstants.SYMBOL_PATTERN.match(symbol))

    def _init_symbol(self, symbol: str, callback: Optional[Callback]) -> bool:
        """
        Initialize tracking for a symbol.
        
        Returns True if this is a new symbol.
        """
        if not self._validate_symbol(symbol):
            raise ValueError(f"Invalid symbol format: {symbol}")

        with self._lock:
            is_new = symbol not in self._analyzers

            if is_new:
                self._analyzers[symbol] = OrderBookAnalyzer()
                self._callbacks[symbol] = []
                self._health[symbol] = StreamHealth()

            if callback and callback not in self._callbacks[symbol]:
                self._callbacks[symbol].append(callback)

            return is_new

    def _remove_symbol(self, symbol: str) -> None:
        """Remove symbol from tracking."""
        with self._lock:
            self._analyzers.pop(symbol, None)
            self._callbacks.pop(symbol, None)
            self._health.pop(symbol, None)

    def get_analyzer(self, symbol: str) -> Optional[OrderBookAnalyzer]:
        """Get analyzer for symbol."""
        with self._lock:
            return self._analyzers.get(symbol)

    def get_snapshot(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """Get latest snapshot for symbol."""
        analyzer = self.get_analyzer(symbol)
        return analyzer.current if analyzer else None

    def get_health(self, symbol: str) -> Optional[StreamHealth]:
        """Get health metrics for symbol."""
        with self._lock:
            return self._health.get(symbol)

    def get_all_health(self) -> Dict[str, StreamHealth]:
        """Get health metrics for all symbols."""
        with self._lock:
            return dict(self._health)

    def _update_health(
        self,
        symbol: str,
        connected: bool = True,
        error: Optional[str] = None,
        reconnect: bool = False
    ) -> None:
        """Update health status for a symbol."""
        with self._lock:
            if symbol not in self._health:
                return

            h = self._health[symbol]
            h.connected = connected

            if error:
                h.record_error(error)
            elif connected:
                h.reset_errors()

            if reconnect:
                h.record_reconnect()

    def _record_update(self, symbol: str, timestamp: int) -> None:
        """Record an update for health tracking."""
        now_ms = _now_ms()
        with self._lock:
            if symbol in self._health:
                self._health[symbol].last_update_ms = now_ms
                if timestamp > 0:
                    self._health[symbol].latency_ms = now_ms - timestamp

    def _process_snapshot(
        self,
        symbol: str,
        snapshot: OrderBookSnapshot,
        timestamp: int
    ) -> None:
        """Process a new snapshot (add to analyzer, invoke callbacks)."""
        # Add to analyzer and record update
        with self._lock:
            if symbol in self._analyzers:
                self._analyzers[symbol].add_snapshot(snapshot)
            self._record_update(symbol, timestamp)
            callbacks = list(self._callbacks.get(symbol, []))

        # Invoke callbacks outside lock
        for cb in callbacks:
            try:
                cb(snapshot)
            except Exception as e:
                logger.error(f"Callback error for {symbol}: {e}")

    def _start_background(self, target: Callable[[], None]) -> None:
        """Start background thread for event loop."""
        with self._init_lock:
            if self._running:
                return

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=target, daemon=True)
            self._running = True
            self._thread.start()

    def _run_loop_base(
        self,
        coro_factory: Optional[Callable[[], Any]] = None
    ) -> None:
        """Run the event loop (called in background thread)."""
        asyncio.set_event_loop(self._loop)
        self._started_event.set()

        try:
            if coro_factory:
                self._loop.run_until_complete(coro_factory())
            else:
                # Run until stopped
                async def run_until_stopped():
                    while self._running:
                        await asyncio.sleep(0.1)

                self._loop.run_until_complete(run_until_stopped())

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._running:
                logger.error(f"Event loop error: {e}")
        finally:
            self._cleanup_loop()

    def _cleanup_loop(self) -> None:
        """Gracefully clean up event loop."""
        if not self._loop or self._loop.is_closed():
            return

        try:
            # Cancel all pending tasks
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()

                # Wait for cancellations
                if pending and not self._loop.is_closed():
                    try:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    except (RuntimeError, OSError, asyncio.CancelledError):
                        pass
            except RuntimeError:
                pass

            # Final brief pause for transport cleanup
            if not self._loop.is_closed():
                try:
                    self._loop.run_until_complete(asyncio.sleep(0.01))
                except (RuntimeError, OSError, asyncio.CancelledError):
                    pass

        except Exception:
            pass
        finally:
            try:
                if not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass
            self._loop = None

    def _stop_base(self, tasks: Optional[List[asyncio.Task]] = None) -> None:
        """Stop the stream gracefully."""
        self._running = False

        # Mark all as disconnected
        with self._lock:
            for h in self._health.values():
                h.connected = False

        # Cancel tasks
        if tasks:
            for task in tasks:
                if hasattr(task, 'cancel'):
                    task.cancel()

        # Signal loop to stop
        if self._loop and not self._loop.is_closed():
            try:
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

        # Wait for thread
        if self._thread and self._thread.is_alive():
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=OrderBookConstants.SHUTDOWN_TIMEOUT)

        self._started_event.clear()


# ============================================================
# ♦️BINANCE SINGLE SYMBOL STREAM
# ============================================================
class BinanceOrderBookStream(_StreamBase):
    """
    WebSocket stream for a single Binance symbol.
    
    For multiple symbols, use BinanceMultiStream which is more efficient.
    """

    def __init__(
        self,
        max_reconnect_attempts: int = OrderBookConstants.MAX_RECONNECT_ATTEMPTS,
        testnet: bool = False,
        alerts: Optional[AlertManager] = None
    ):
        """
        Initialize Binance stream.
        
        Args:
            max_reconnect_attempts: Max reconnection attempts before giving up
            testnet: Use testnet endpoint
            alerts: Optional alert manager for notifications
        """
        super().__init__()

        self._base_url = (
            "wss://testnet.binance.vision/ws" if testnet
            else "wss://stream.binance.com:9443/ws"
        )
        self._max_reconnect = max_reconnect_attempts
        self._tasks: Dict[str, asyncio.Task] = {}
        self._validation_failures: Dict[str, int] = {}
        self._alerts = alerts

    def subscribe(self, symbol: str, callback: Optional[Callback] = None) -> None:
        """Subscribe to order book updates for a symbol."""
        self._init_symbol(symbol, callback)

        with self._init_lock:
            if not self._running:
                self._start_background(self._run_loop)
                self._started_event.wait(timeout=5.0)

        if self._loop and self._running:
            ws_symbol = symbol.replace("/", "").replace("-", "").lower()
            asyncio.run_coroutine_threadsafe(
                self._add_subscription(symbol, ws_symbol),
                self._loop
            )

    async def _add_subscription(self, symbol: str, ws_symbol: str) -> None:
        """Add subscription task for symbol."""
        if symbol not in self._tasks or self._tasks[symbol].done():
            self._tasks[symbol] = asyncio.create_task(
                self._subscribe_stream(symbol, ws_symbol)
            )

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        with self._lock:
            if symbol in self._tasks:
                self._tasks[symbol].cancel()
                del self._tasks[symbol]
        self._remove_symbol(symbol)

    @property
    def subscribed_symbols(self) -> List[str]:
        """Get list of subscribed symbols."""
        with self._lock:
            return list(self._analyzers.keys())

    def _run_loop(self) -> None:
        """Run the event loop."""
        self._run_loop_base(self._main_loop)

    async def _main_loop(self) -> None:
        """Main async loop."""
        while self._running:
            await asyncio.sleep(1)

    async def _subscribe_stream(self, symbol: str, ws_symbol: str) -> None:
        """Subscribe to a single symbol stream with reconnection."""
        if not HAS_WEBSOCKETS:
            logger.error("websockets library not installed")
            return

        url = f"{self._base_url}/{ws_symbol}@depth20@100ms"
        reconnect_attempts = 0
        backoff = OrderBookConstants.INITIAL_BACKOFF

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=OrderBookConstants.WS_PING_INTERVAL,
                    ping_timeout=OrderBookConstants.WS_PING_TIMEOUT,
                    close_timeout=OrderBookConstants.WS_CLOSE_TIMEOUT
                ) as ws:
                    logger.info(f"Connected to {symbol}")
                    reconnect_attempts = 0
                    backoff = OrderBookConstants.INITIAL_BACKOFF
                    self._update_health(symbol, connected=True)

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(
                                ws.recv(),
                                timeout=OrderBookConstants.WS_RECV_TIMEOUT
                            )
                            self._process_message(symbol, json.loads(msg))

                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            return
                        except ConnectionClosed:
                            logger.warning(f"{symbol} connection closed")
                            break
                        except json.JSONDecodeError as e:
                            logger.warning(f"Invalid JSON from {symbol}: {e}")
                        except Exception as e:
                            logger.error(f"Message error for {symbol}: {e}")
                            break

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return

                reconnect_attempts += 1
                self._update_health(
                    symbol,
                    connected=False,
                    error=str(e),
                    reconnect=True
                )

                if reconnect_attempts > self._max_reconnect:
                    logger.error(f"Max reconnect attempts for {symbol}")
                    return

                logger.warning(f"Reconnecting {symbol} in {backoff:.1f}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, OrderBookConstants.MAX_BACKOFF)

    def _process_message(self, symbol: str, data: Dict[str, Any]) -> None:
        """Process a WebSocket message using fast path."""
        try:
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if not bids and not asks:
                return

            timestamp = data.get("E", _now_ms())

            # Use fast Binance path - bypasses validation and sorting
            snapshot = OrderBookSnapshot.from_binance_fast(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=timestamp,
                sequence=data.get("lastUpdateId", 0)
            )

            # Quick validity check (minimal overhead)
            if not snapshot.bids or not snapshot.asks:
                return

            # Check for crossed book (rare but possible during high volatility)
            if snapshot.bids[0].price >= snapshot.asks[0].price:
                self._validation_failures[symbol] = (
                    self._validation_failures.get(symbol, 0) + 1
                )
                return

            self._validation_failures[symbol] = 0
            self._process_snapshot(symbol, snapshot, timestamp)

        except Exception as e:
            logger.error(f"Process error for {symbol}: {e}")

    def stop(self) -> None:
        """Stop the stream."""
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()

        self._stop_base(tasks)
        logger.info("Binance stream stopped")


# ============================================================
# ♦️BINANCE MULTI-SYMBOL STREAM
# ============================================================
class BinanceMultiStream(_StreamBase):
    """
    Efficient multi-symbol WebSocket stream for Binance.
    
    Uses combined streams (up to 100 symbols per connection).
    More efficient than individual connections for multiple symbols.
    
    Optimizations:
    - Pre-compiled symbol mappings for O(1) lookup
    - Fast Binance parsing path (no validation overhead)
    - Minimal string operations in hot path
    """

    MAX_STREAMS_PER_CONNECTION: Final[int] = 100
    MAX_CONNECTIONS: Final[int] = 5

    def __init__(
        self,
        testnet: bool = False,
        alerts: Optional[AlertManager] = None
    ):
        """
        Initialize multi-symbol stream.
        
        Args:
            testnet: Use testnet endpoint
            alerts: Optional alert manager
        """
        super().__init__()

        self._base_url = (
            "wss://testnet.binance.vision/stream" if testnet
            else "wss://stream.binance.com:9443/stream"
        )
        self._symbols: Set[str] = set()
        self._connection_tasks: List[asyncio.Task] = []
        self._update_counts: Dict[str, int] = {}
        self._last_health_calc = time.time()
        
        # Pre-compiled symbol mappings for O(1) lookup
        self._symbol_to_ws: Dict[str, str] = {}       # "BTCUSDT" -> "BTC/USDT"
        self._stream_to_symbol: Dict[str, str] = {}   # "btcusdt@depth20@100ms" -> "BTC/USDT"
        
        self._msg_count = 0
        self._alerts = alerts
        self._validation_failures: Dict[str, int] = {}
        self._symbols_changed = threading.Event()

    @property
    def max_capacity(self) -> int:
        """Maximum number of symbols supported."""
        return self.MAX_STREAMS_PER_CONNECTION * self.MAX_CONNECTIONS

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        with self._lock:
            return len([t for t in self._connection_tasks if not t.done()])

    @property
    def subscribed_count(self) -> int:
        """Number of subscribed symbols."""
        with self._lock:
            return len(self._symbols)

    def _chunk_symbols(self, symbols: List[str]) -> List[List[str]]:
        """Split symbols into chunks for multiple connections."""
        chunks: List[List[str]] = []

        for i in range(0, len(symbols), self.MAX_STREAMS_PER_CONNECTION):
            chunk = symbols[i:i + self.MAX_STREAMS_PER_CONNECTION]
            chunks.append(chunk)
            if len(chunks) >= self.MAX_CONNECTIONS:
                break

        return chunks

    def _build_stream_url(self, symbols: List[str]) -> str:
        """
        Build combined stream URL.
        
        Truncates if URL would be too long, with warning.
        """
        original_count = len(symbols)
        symbols = list(symbols)  # Don't modify original

        while symbols:
            streams = "/".join(
                f"{s.replace('/', '').replace('-', '').lower()}@depth20@100ms"
                for s in symbols
            )
            url = f"{self._base_url}?streams={streams}"

            if len(url) <= OrderBookConstants.MAX_URL_LENGTH:
                if len(symbols) < original_count:
                    logger.warning(
                        f"URL length limit: dropped {original_count - len(symbols)} "
                        f"symbols from connection"
                    )
                return url

            symbols = symbols[:-1]

        logger.error("Could not build stream URL - all symbols dropped")
        return f"{self._base_url}?streams="

    def _subscribe_internal(
        self,
        symbols: List[str],
        callback: Optional[Callback]
    ) -> None:
        """Internal subscribe implementation with pre-compiled mappings."""
        # Validate all symbols
        invalid = [s for s in symbols if not self._validate_symbol(s)]
        if invalid:
            raise ValueError(f"Invalid symbols: {invalid}")

        with self._lock:
            # Check capacity
            new_symbols = [s for s in symbols if s not in self._symbols]
            new_count = len(self._symbols) + len(new_symbols)

            if new_count > self.max_capacity:
                raise ValueError(
                    f"Exceeds capacity: {new_count} > {self.max_capacity}"
                )

            # Initialize each symbol with pre-compiled mappings
            for symbol in symbols:
                if symbol not in self._analyzers:
                    self._analyzers[symbol] = OrderBookAnalyzer()
                    self._callbacks[symbol] = []
                    self._health[symbol] = StreamHealth()
                    
                    # Pre-compile ALL mappings at subscription time
                    # This avoids string operations on every message
                    clean_lower = symbol.replace("/", "").replace("-", "").lower()
                    clean_upper = clean_lower.upper()
                    stream_key = f"{clean_lower}@depth20@100ms"
                    
                    self._symbol_to_ws[clean_upper] = symbol
                    self._stream_to_symbol[stream_key] = symbol  # O(1) direct lookup

                if callback and callback not in self._callbacks[symbol]:
                    self._callbacks[symbol].append(callback)

                self._symbols.add(symbol)

        self._symbols_changed.set()

        # Start if not running
        with self._init_lock:
            if not self._running:
                self._start_background(self._run_loop)
                self._started_event.wait(timeout=5.0)

    def subscribe(self, symbol: str, callback: Optional[Callback] = None) -> None:
        """Subscribe to a single symbol."""
        self._subscribe_internal([symbol], callback)

    def subscribe_many(
        self,
        symbols: List[str],
        callback: Optional[Callback] = None
    ) -> None:
        """Subscribe to multiple symbols."""
        self._subscribe_internal(symbols, callback)

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        with self._lock:
            self._symbols.discard(symbol)
            
            # Clean up all mappings
            clean_lower = symbol.replace("/", "").replace("-", "").lower()
            clean_upper = clean_lower.upper()
            stream_key = f"{clean_lower}@depth20@100ms"
            
            self._symbol_to_ws.pop(clean_upper, None)
            self._stream_to_symbol.pop(stream_key, None)

        self._remove_symbol(symbol)
        self._symbols_changed.set()

    def get_all_snapshots(self) -> Dict[str, Optional[OrderBookSnapshot]]:
        """Get latest snapshot for all symbols."""
        with self._lock:
            return {
                s: self._analyzers[s].current
                for s in self._analyzers
            }

    def get_connection_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        with self._lock:
            symbols_list = list(self._symbols)

        chunks = self._chunk_symbols(symbols_list)

        return {
            "total_symbols": len(symbols_list),
            "max_capacity": self.max_capacity,
            "utilization_pct": len(symbols_list) / self.max_capacity * 100,
            "connections_needed": len(chunks),
            "connections_active": self.connection_count,
            "max_connections": self.MAX_CONNECTIONS,
            "symbols_per_connection": self.MAX_STREAMS_PER_CONNECTION,
            "chunks": [len(c) for c in chunks],
        }

    def _run_loop(self) -> None:
        """Run the event loop."""
        self._run_loop_base(self._stream_loop)

    async def _stream_loop(self) -> None:
        """Main streaming loop."""
        if not HAS_WEBSOCKETS:
            logger.error("websockets library not installed")
            return

        current_symbols: FrozenSet[str] = frozenset()

        try:
            while self._running:
                with self._lock:
                    new_symbols = frozenset(self._symbols)

                if not new_symbols:
                    await asyncio.sleep(1)
                    continue

                # Reconnect if symbols changed
                if new_symbols != current_symbols or self._symbols_changed.is_set():
                    self._symbols_changed.clear()
                    current_symbols = new_symbols

                    # Cancel existing connections
                    for task in self._connection_tasks:
                        if not task.done():
                            task.cancel()

                    if self._connection_tasks:
                        try:
                            await asyncio.gather(
                                *self._connection_tasks,
                                return_exceptions=True
                            )
                        except Exception:
                            pass
                        self._connection_tasks.clear()

                    # Start new connections
                    chunks = self._chunk_symbols(list(current_symbols))
                    self._connection_tasks = [
                        asyncio.create_task(
                            self._manage_connection(i, list(c))
                        )
                        for i, c in enumerate(chunks)
                    ]
                    logger.info(
                        f"Started {len(chunks)} connections for "
                        f"{len(current_symbols)} symbols"
                    )

                await asyncio.sleep(OrderBookConstants.CONNECTION_CHECK_INTERVAL)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._running:
                logger.error(f"Stream loop error: {e}")

    async def _manage_connection(
        self,
        conn_id: int,
        symbols: List[str]
    ) -> None:
        """Manage a single WebSocket connection with reconnection."""
        backoff = 1.0

        while self._running:
            try:
                url = self._build_stream_url(symbols)

                async with websockets.connect(
                    url,
                    ping_interval=OrderBookConstants.WS_PING_INTERVAL,
                    ping_timeout=OrderBookConstants.WS_PING_TIMEOUT,
                    close_timeout=OrderBookConstants.WS_CLOSE_TIMEOUT,
                    max_size=OrderBookConstants.WS_MAX_MESSAGE_SIZE
                ) as ws:
                    logger.info(f"Connection {conn_id}: {len(symbols)} symbols")
                    backoff = 1.0

                    # Mark all connected
                    for s in symbols:
                        self._update_health(s, connected=True)

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(
                                ws.recv(),
                                timeout=OrderBookConstants.WS_RECV_TIMEOUT
                            )
                            self._process_combined_message(json.loads(msg))

                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            return
                        except ConnectionClosed:
                            logger.warning(f"Connection {conn_id}: closed")
                            break
                        except Exception as e:
                            logger.debug(f"Connection {conn_id}: {e}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return

                # Mark disconnected
                for s in symbols:
                    self._update_health(
                        s,
                        connected=False,
                        error=str(e),
                        reconnect=True
                    )

                logger.warning(
                    f"Connection {conn_id}: reconnecting in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, OrderBookConstants.MAX_BACKOFF)

    def _process_combined_message(self, data: Dict[str, Any]) -> None:
        """Process a combined stream message with O(1) symbol lookup."""
        try:
            stream = data.get("stream", "")
            payload = data.get("data", {})
            
            # O(1) direct lookup - no string splitting or upper() needed
            symbol = self._stream_to_symbol.get(stream)
            if not symbol:
                return

            bids = payload.get("bids", [])
            asks = payload.get("asks", [])

            if not bids and not asks:
                return

            timestamp = payload.get("E", _now_ms())
            now = time.time()

            # Use fast Binance path - bypasses validation and sorting
            snapshot = OrderBookSnapshot.from_binance_fast(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=timestamp,
                sequence=payload.get("lastUpdateId", 0)
            )

            # Quick validity check (minimal overhead)
            if not snapshot.bids or not snapshot.asks:
                return

            # Check for crossed book (rare but possible during high volatility)
            if snapshot.bids[0].price >= snapshot.asks[0].price:
                self._validation_failures[symbol] = (
                    self._validation_failures.get(symbol, 0) + 1
                )
                return

            self._validation_failures[symbol] = 0

            # Update state
            with self._lock:
                if symbol in self._analyzers:
                    self._analyzers[symbol].add_snapshot(snapshot)

                if symbol in self._health:
                    self._health[symbol].last_update_ms = int(now * 1000)
                    if timestamp > 0:
                        self._health[symbol].latency_ms = now * 1000 - timestamp

                # Update counts
                self._update_counts[symbol] = (
                    self._update_counts.get(symbol, 0) + 1
                )
                self._msg_count += 1

                # Periodic health calculation
                if now - self._last_health_calc >= OrderBookConstants.HEALTH_CALC_INTERVAL:
                    elapsed = now - self._last_health_calc
                    for sym, cnt in self._update_counts.items():
                        if sym in self._health:
                            self._health[sym].updates_per_second = cnt / elapsed
                    self._update_counts.clear()
                    self._last_health_calc = now

                callbacks = list(self._callbacks.get(symbol, []))

            # Invoke callbacks
            for cb in callbacks:
                try:
                    cb(snapshot)
                except Exception as e:
                    logger.error(f"Callback error for {symbol}: {e}")

        except Exception as e:
            logger.debug(f"Process error: {e}")

    async def _wait_for_tasks(self) -> None:
        """Wait for connection tasks to complete."""
        if self._connection_tasks:
            try:
                await asyncio.wait(
                    self._connection_tasks,
                    timeout=2.0,
                    return_when=asyncio.ALL_COMPLETED
                )
            except Exception:
                pass
        await asyncio.sleep(0.1)

    def stop(self) -> None:
        """Stop all connections gracefully."""
        self._running = False

        with self._lock:
            for h in self._health.values():
                h.connected = False

        # Cancel all tasks
        for task in self._connection_tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks
        if self._loop and not self._loop.is_closed() and self._connection_tasks:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._wait_for_tasks(),
                    self._loop
                )
                future.result(timeout=3.0)
            except Exception:
                pass

        self._stop_base(self._connection_tasks)
        self._connection_tasks.clear()
        logger.info("Multi-stream stopped")

    def get_pool_health(self) -> Dict[str, Any]:
        """Get aggregate health across all connections."""
        all_health = self.get_all_health()

        if not all_health:
            return {
                "status": "no_connections",
                "total_symbols": 0,
                "connected": 0,
                "healthy": 0,
                "avg_latency_ms": 0,
                "p50_latency_ms": 0,
                "p99_latency_ms": 0,
                "total_errors": 0,
                "total_reconnects": 0,
                "updates_per_second": 0,
            }

        # Collect metrics
        latencies = sorted([
            h.latency_ms for h in all_health.values()
            if h.latency_ms > 0
        ]) or [0]

        connected_count = sum(1 for h in all_health.values() if h.connected)
        healthy_count = sum(1 for h in all_health.values() if h.is_healthy)
        total_symbols = len(all_health)

        # Determine status
        if healthy_count == total_symbols:
            status = "healthy"
        elif healthy_count >= total_symbols * 0.9:
            status = "good"
        elif healthy_count >= total_symbols * 0.5:
            status = "degraded"
        elif connected_count > 0:
            status = "critical"
        else:
            status = "down"

        def percentile(lst: List[float], pct: float) -> float:
            if not lst:
                return 0.0
            idx = min(int(len(lst) * pct / 100), len(lst) - 1)
            return lst[idx]

        return {
            "status": status,
            "total_symbols": total_symbols,
            "connected": connected_count,
            "healthy": healthy_count,
            "unhealthy_symbols": [
                s for s, h in all_health.items()
                if not h.is_healthy
            ][:10],
            "avg_latency_ms": sum(latencies) / len(latencies) if latencies else 0,
            "p50_latency_ms": percentile(latencies, 50),
            "p99_latency_ms": percentile(latencies, 99),
            "max_latency_ms": max(latencies) if latencies else 0,
            "total_errors": sum(h.error_count for h in all_health.values()),
            "total_reconnects": sum(
                h.reconnect_count for h in all_health.values()
            ),
            "updates_per_second": round(
                sum(h.updates_per_second for h in all_health.values()), 2
            ),
            "connections_active": self.connection_count,
            "connections_max": self.MAX_CONNECTIONS,
        }


# ============================================================
# 🎃CCXT PRO STREAM (OPTIONAL)
# ============================================================

class CCXTOrderBookStream(_StreamBase):
    """
    Order book stream using CCXT Pro.
    
    Supports multiple exchanges with a unified interface.
    Requires ccxt.pro to be installed.
    """

    def __init__(self, exchange_id: str = "binance"):
        """
        Initialize CCXT stream.
        
        Args:
            exchange_id: CCXT exchange identifier
            
        Raises:
            ImportError: If ccxt.pro is not installed
        """
        if not HAS_CCXT_PRO:
            raise ImportError(
                "ccxt.pro not installed. Install with: pip install ccxt"
            )

        super().__init__()
        self.exchange_id = exchange_id
        self._exchange = None
        self._symbol_tasks: Dict[str, asyncio.Task] = {}

    def subscribe(self, symbol: str, callback: Optional[Callback] = None) -> None:
        """Subscribe to order book updates."""
        self._init_symbol(symbol, callback)

        with self._init_lock:
            if not self._running:
                self._start_background(self._run_loop)
                self._started_event.wait(timeout=5.0)

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        self._remove_symbol(symbol)

    def _run_loop(self) -> None:
        """Run the event loop."""
        asyncio.set_event_loop(self._loop)
        self._started_event.set()

        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as e:
            logger.error(f"CCXT loop error: {e}")
        finally:
            if self._exchange:
                try:
                    self._loop.run_until_complete(self._exchange.close())
                except Exception:
                    pass
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main_loop(self) -> None:
        """Main async loop."""
        self._exchange = getattr(ccxtpro, self.exchange_id)({
            'enableRateLimit': True
        })

        try:
            while self._running:
                with self._lock:
                    current = set(self._analyzers.keys())

                # Start new symbol tasks
                for s in current:
                    if s not in self._symbol_tasks or self._symbol_tasks[s].done():
                        self._symbol_tasks[s] = asyncio.create_task(
                            self._watch_symbol(s)
                        )

                # Cancel removed symbol tasks
                for s in list(self._symbol_tasks.keys()):
                    if s not in current:
                        self._symbol_tasks[s].cancel()
                        try:
                            await self._symbol_tasks[s]
                        except asyncio.CancelledError:
                            pass
                        del self._symbol_tasks[s]

                await asyncio.sleep(0.1)

        finally:
            # Cleanup
            for task in self._symbol_tasks.values():
                task.cancel()

            if self._symbol_tasks:
                try:
                    await asyncio.gather(
                        *self._symbol_tasks.values(),
                        return_exceptions=True
                    )
                except Exception:
                    pass

            if self._exchange:
                await self._exchange.close()

    async def _watch_symbol(self, symbol: str) -> None:
        """Watch a single symbol."""
        while self._running:
            try:
                start = time.time()
                ob = await self._exchange.watch_order_book(symbol, limit=20)
                timestamp = ob.get('timestamp', _now_ms())

                snapshot = OrderBookSnapshot.from_raw(
                    symbol=symbol,
                    bids=ob.get('bids', [])[:20],
                    asks=ob.get('asks', [])[:20],
                    timestamp=timestamp,
                    exchange=self.exchange_id
                )

                self._update_health(symbol, connected=True)
                self._process_snapshot(symbol, snapshot, timestamp)

                # Rate limit
                elapsed = time.time() - start
                if elapsed < OrderBookConstants.CCXT_MIN_WATCH_INTERVAL:
                    await asyncio.sleep(
                        OrderBookConstants.CCXT_MIN_WATCH_INTERVAL - elapsed
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Watch error for {symbol}: {e}")
                self._update_health(symbol, connected=False, error=str(e))
                await asyncio.sleep(1)

    def stop(self) -> None:
        """Stop the stream."""
        self._running = False

        with self._lock:
            for h in self._health.values():
                h.connected = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        self._started_event.clear()


# ============================================================
# TRADING SYMBOLS
# ============================================================

ALL_TRADING_SYMBOLS: Final[List[str]] = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
    "DOGE/USDT", "ADA/USDT", "TRX/USDT", "AVAX/USDT", "SHIB/USDT",
    "DOT/USDT", "LINK/USDT", "TON/USDT", "SUI/USDT", "XLM/USDT",
    "BCH/USDT", "HBAR/USDT", "LTC/USDT", "UNI/USDT", "PEPE/USDT",
    "NEAR/USDT", "APT/USDT", "ICP/USDT", "ETC/USDT", "POL/USDT",
    "FET/USDT", "RENDER/USDT", "ARB/USDT", "OP/USDT", "INJ/USDT",
    "FIL/USDT", "ATOM/USDT", "WIF/USDT", "IMX/USDT", "STX/USDT",
    "GRT/USDT", "TAO/USDT", "BONK/USDT", "SEI/USDT", "JUP/USDT",
    "FLOKI/USDT", "TIA/USDT", "ORDI/USDT", "WLD/USDT", "ALGO/USDT",
    "FTM/USDT", "FLOW/USDT", "AAVE/USDT", "SAND/USDT", "MANA/USDT",
    "AXS/USDT", "GALA/USDT", "THETA/USDT", "RUNE/USDT", "EGLD/USDT",
    "EOS/USDT", "XTZ/USDT", "CFX/USDT", "ROSE/USDT", "KAVA/USDT",
    "ZIL/USDT", "BLUR/USDT", "MEME/USDT", "PYTH/USDT", "SUPER/USDT",
    "JASMY/USDT", "ENS/USDT", "CHZ/USDT", "LDO/USDT", "CRV/USDT",
    "SNX/USDT", "CAKE/USDT", "COMP/USDT", "1INCH/USDT", "MASK/USDT",
    "BAT/USDT", "ZRX/USDT", "SUSHI/USDT", "YFI/USDT", "MKR/USDT",
]


# ============================================================
# ORDER BOOK MANAGER
# ============================================================

class OrderBookManager:
    """
    High-level order book management.
    
    Provides:
    - Unified interface for live and backtest modes
    - Automatic stream selection (single vs multi-symbol)
    - History storage with compression
    - Validation and alerting
    - Health monitoring
    """

    def __init__(
        self,
        exchange: str = "binance",
        use_ccxt_pro: bool = False,
        store_history: bool = False,
        history_dir: str = "orderbook_history",
        testnet: bool = False,
        multi_symbol: bool = True,
        enable_validation: bool = False,
        enable_alerts: bool = True,
        stale_data_ms: int = OrderBookConstants.STALE_DATA_MS,
        backtest_mode: bool = False
    ):
        """
        Initialize order book manager.
        
        Args:
            exchange: Exchange identifier
            use_ccxt_pro: Use CCXT Pro instead of native WebSocket
            store_history: Enable history storage to disk
            history_dir: Directory for history files
            testnet: Use testnet endpoints
            multi_symbol: Use multi-symbol stream (more efficient)
            enable_validation: Enable data validation
            enable_alerts: Enable alerting
            stale_data_ms: Stale data threshold in ms
            backtest_mode: Enable backtest mode (no live connections)
        """
        self.exchange = exchange
        self.store_history = store_history
        self.backtest_mode = backtest_mode
        self.history_dir = Path(history_dir)
        self._stale_data_ms = stale_data_ms
        self._lock = threading.RLock()
        self._flush_size = OrderBookConstants.HISTORY_FLUSH_SIZE
        self._history_buffers: Dict[str, List[OrderBookSnapshot]] = {}
        self._last_health_report = 0.0
        self._flush_executor: Optional[ThreadPoolExecutor] = None

        # Setup history storage
        if store_history:
            self.history_dir.mkdir(exist_ok=True, parents=True)
            self._flush_executor = ThreadPoolExecutor(
                max_workers=OrderBookConstants.MAX_FLUSH_THREADS
            )

        # Setup validation
        self._validator = DataValidator() if enable_validation else None

        # Setup alerts
        self._alerts = AlertManager() if enable_alerts else None
        if self._alerts:
            self._alerts.add_handler(console_alert_handler)

        # Setup state
        self._state = StateManager()

        # Setup stream or backtest provider
        self._stream: Optional[BaseOrderBookStream] = None
        self._backtest_provider: Optional[BacktestOrderBookProvider] = None

        if backtest_mode:
            self._backtest_provider = BacktestOrderBookProvider()
        else:
            self._stream = self._create_stream(use_ccxt_pro, multi_symbol, testnet)

    def _create_stream(
        self,
        use_ccxt_pro: bool,
        multi_symbol: bool,
        testnet: bool
    ) -> BaseOrderBookStream:
        """Create appropriate stream based on configuration."""
        if use_ccxt_pro and HAS_CCXT_PRO:
            return CCXTOrderBookStream(self.exchange)
        elif multi_symbol and HAS_WEBSOCKETS:
            return BinanceMultiStream(testnet=testnet, alerts=self._alerts)
        elif HAS_WEBSOCKETS:
            return BinanceOrderBookStream(testnet=testnet, alerts=self._alerts)
        else:
            raise ImportError(
                "No WebSocket library available. "
                "Install websockets: pip install websockets"
            )

    def add_alert_handler(
        self,
        handler: Callable[[str, str, Dict[str, Any]], None]
    ) -> None:
        """Add custom alert handler."""
        if self._alerts:
            self._alerts.add_handler(handler)

    def subscribe(self, symbol: str, callback: Optional[Callback] = None) -> None:
        """Subscribe to a single symbol."""
        if self.backtest_mode:
            logger.warning("Cannot subscribe in backtest mode")
            return

        self._state.set_running(True)

        if self._stream:
            # Wrap callback to include our processing
            wrapped = lambda s: self._handle_snapshot(s, callback)
            self._stream.subscribe(symbol, wrapped)

    def subscribe_many(
        self,
        symbols: List[str],
        callback: Optional[Callback] = None
    ) -> None:
        """Subscribe to multiple symbols."""
        if self.backtest_mode:
            logger.warning("Cannot subscribe in backtest mode")
            return

        self._state.set_running(True)
        wrapped = lambda s: self._handle_snapshot(s, callback)

        if hasattr(self._stream, 'subscribe_many'):
            self._stream.subscribe_many(symbols, wrapped)
        else:
            for sym in symbols:
                self._stream.subscribe(sym, wrapped)

    def _handle_snapshot(
        self,
        snapshot: OrderBookSnapshot,
        user_callback: Optional[Callback]
    ) -> None:
        """Handle incoming snapshot with validation and storage."""
        symbol = snapshot.symbol

        # Validate
        if self._validator:
            is_valid, warnings = self._validator.validate(snapshot)

            for w in warnings:
                logger.debug(f"[{symbol}] Validation: {w}")
                if self._alerts:
                    self._alerts.warning(
                        w,
                        {"symbol": symbol},
                        throttle_key=f"val_{symbol}_{w[:20]}"
                    )

            if not is_valid:
                return

        # Store history
        if self.store_history:
            self._buffer_snapshot(symbol, snapshot)

        # Periodic health report
        now = time.time()
        if now - self._last_health_report >= OrderBookConstants.HEALTH_REPORT_INTERVAL:
            self._report_health()
            self._last_health_report = now

        # User callback
        if user_callback:
            try:
                user_callback(snapshot)
            except Exception as e:
                logger.error(f"User callback error for {symbol}: {e}")

    def _buffer_snapshot(
        self,
        symbol: str,
        snapshot: OrderBookSnapshot
    ) -> None:
        """Buffer snapshot for history storage."""
        buf = None

        with self._lock:
            if symbol not in self._history_buffers:
                self._history_buffers[symbol] = []

            self._history_buffers[symbol].append(snapshot)

            if len(self._history_buffers[symbol]) >= self._flush_size:
                buf = self._history_buffers[symbol].copy()
                self._history_buffers[symbol] = []

        if buf and self._flush_executor:
            self._flush_executor.submit(self._do_flush, symbol, buf)

    def _report_health(self) -> None:
        """Log periodic health report."""
        if not self._stream:
            return

        health = self._stream.get_all_health()
        if not health:
            return

        healthy = sum(1 for h in health.values() if h.is_healthy)
        total = len(health)
        logger.info(f"Stream health: {healthy}/{total} symbols healthy")

        if healthy < total and self._alerts:
            unhealthy = [s for s, h in health.items() if not h.is_healthy][:10]
            self._alerts.warning(
                f"{total - healthy} unhealthy streams",
                {"symbols": unhealthy},
                throttle_key="unhealthy"
            )

    def _do_flush(
        self,
        symbol: str,
        snapshots: List[OrderBookSnapshot]
    ) -> None:
        """Flush snapshots to disk with compression."""
        safe_sym = symbol.replace("/", "_").replace("-", "_")
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = self.history_dir / f"ob_{safe_sym}_{ts}.pkl.gz"

        try:
            with gzip.open(filename, 'wb') as f:
                pickle.dump([s.to_dict() for s in snapshots], f)
            logger.info(f"Saved {len(snapshots)} snapshots to {filename}")
        except Exception as e:
            logger.error(f"Failed to save history: {e}")
            # Put snapshots back in buffer
            with self._lock:
                existing = self._history_buffers.get(symbol, [])
                self._history_buffers[symbol] = snapshots + existing

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        if self.backtest_mode:
            self._backtest_provider.clear(symbol)
            return

        # Flush remaining history
        if self.store_history and symbol in self._history_buffers:
            with self._lock:
                buf = self._history_buffers.pop(symbol, [])
            if buf:
                self._do_flush(symbol, buf)

        if self._stream:
            self._stream.unsubscribe(symbol)

    # ─────────────────────────────────────────────────────────
    # BACKTEST METHODS
    # ─────────────────────────────────────────────────────────

    def load_backtest_snapshots(
        self,
        symbol: str,
        snapshots: List[OrderBookSnapshot]
    ) -> None:
        """Load pre-recorded snapshots for backtesting."""
        if not self.backtest_mode:
            raise RuntimeError("Not in backtest mode")
        self._backtest_provider.load_snapshots(symbol, snapshots)

    def load_backtest_ticks(
        self,
        symbol: str,
        ticks: List[TickData],
        tick_sizes: Optional[List[float]] = None,
        **kwargs
    ) -> None:
        """Load tick data for synthetic order book generation."""
        if not self.backtest_mode:
            raise RuntimeError("Not in backtest mode")
        self._backtest_provider.load_from_ticks(symbol, ticks, tick_sizes, **kwargs)

    def load_backtest_candles(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        **kwargs
    ) -> None:
        """Load candle data for synthetic order book generation."""
        if not self.backtest_mode:
            raise RuntimeError("Not in backtest mode")
        self._backtest_provider.load_from_candles(symbol, candles, **kwargs)

    def set_backtest_time(self, timestamp_ms: int) -> None:
        """Set current backtest time."""
        if not self.backtest_mode:
            raise RuntimeError("Not in backtest mode")
        self._backtest_provider.set_time(timestamp_ms)

    def get_backtest_snapshot(
        self,
        symbol: str,
        timestamp_ms: Optional[int] = None
    ) -> Optional[OrderBookSnapshot]:
        """Get snapshot at specific backtest time."""
        if not self.backtest_mode:
            raise RuntimeError("Not in backtest mode")

        if timestamp_ms:
            return self._backtest_provider.get_snapshot_at_time(symbol, timestamp_ms)
        return self._backtest_provider.get_snapshot(symbol)

    # ─────────────────────────────────────────────────────────
    # DATA ACCESS
    # ─────────────────────────────────────────────────────────

    def get_analyzer(self, symbol: str) -> Optional[OrderBookAnalyzer]:
        """Get analyzer for symbol."""
        if self.backtest_mode:
            return self._backtest_provider.get_analyzer(symbol)
        return self._stream.get_analyzer(symbol) if self._stream else None

    def get_snapshot(self, symbol: str) -> Optional[OrderBookSnapshot]:
        """Get latest snapshot for symbol."""
        if self.backtest_mode:
            return self._backtest_provider.get_snapshot(symbol)
        return self._stream.get_snapshot(symbol) if self._stream else None

    def get_health(self, symbol: str) -> Optional[StreamHealth]:
        """Get health metrics for symbol."""
        if self.backtest_mode:
            return None
        return self._stream.get_health(symbol) if self._stream else None

    def get_all_health(self) -> Dict[str, StreamHealth]:
        """Get health metrics for all symbols."""
        if self.backtest_mode:
            return {}
        return self._stream.get_all_health() if self._stream else {}

    def get_health_report(self) -> Dict[str, Any]:
        """Get comprehensive health report."""
        health = self.get_all_health()
        return {
            "total_symbols": len(health),
            "healthy_count": sum(1 for h in health.values() if h.is_healthy),
            "unhealthy_symbols": [
                s for s, h in health.items() if not h.is_healthy
            ],
            "streams": {s: h.to_dict() for s, h in health.items()},
        }

    def is_ready_for_trading(
        self,
        symbols: List[str]
    ) -> Tuple[bool, List[str]]:
        """
        Check if all symbols are ready for trading.
        
        Returns:
            Tuple of (is_ready, list_of_issues)
        """
        issues: List[str] = []

        for sym in symbols:
            analyzer = self.get_analyzer(sym)

            if not analyzer:
                issues.append(f"{sym}: No data")
            elif analyzer.is_empty:
                issues.append(f"{sym}: Empty")
            elif not analyzer.is_data_fresh(self._stale_data_ms):
                issues.append(f"{sym}: Stale")
            elif not analyzer.current or not analyzer.current.is_valid:
                issues.append(f"{sym}: Invalid")
            else:
                h = self.get_health(sym)
                if h and not h.is_healthy:
                    issues.append(f"{sym}: Unhealthy")

        return len(issues) == 0, issues

    def load_history(
        self,
        symbol: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[OrderBookSnapshot]:
        """Load historical snapshots from disk."""
        safe_sym = symbol.replace("/", "_").replace("-", "_")
        all_snaps: List[OrderBookSnapshot] = []

        for f in sorted(self.history_dir.glob(f"ob_{safe_sym}_*.pkl.gz")):
            try:
                with gzip.open(f, 'rb') as fp:
                    snaps = [
                        OrderBookSnapshot.from_dict(d)
                        for d in pickle.load(fp)
                    ]

                # Filter by time range
                if start_time:
                    start_ms = int(start_time.timestamp() * 1000)
                    snaps = [s for s in snaps if s.timestamp >= start_ms]
                if end_time:
                    end_ms = int(end_time.timestamp() * 1000)
                    snaps = [s for s in snaps if s.timestamp <= end_ms]

                all_snaps.extend(snaps)

            except Exception as e:
                logger.error(f"Error loading {f}: {e}")

        return sorted(all_snaps, key=lambda s: s.timestamp)

    def get_pool_health(self) -> Dict[str, Any]:
        """Get aggregate pool health."""
        if self.backtest_mode:
            return {
                "status": "backtest_mode",
                "total_symbols": len(self._backtest_provider.get_all_symbols()),
            }

        if not self._stream:
            return {"status": "no_stream"}

        if hasattr(self._stream, 'get_pool_health'):
            return self._stream.get_pool_health()

        # Fallback for streams without get_pool_health
        all_health = self._stream.get_all_health()
        if not all_health:
            return {"status": "no_connections", "total_symbols": 0}

        healthy_count = sum(1 for h in all_health.values() if h.is_healthy)
        return {
            "status": "healthy" if healthy_count == len(all_health) else "degraded",
            "total_symbols": len(all_health),
            "connected": sum(1 for h in all_health.values() if h.connected),
            "healthy": healthy_count,
            "unhealthy_symbols": [
                s for s, h in all_health.items() if not h.is_healthy
            ],
        }

    def stop(self) -> None:
        """Stop the manager and all streams."""
        self._state.set_running(False)

        # Flush remaining history
        if self.store_history:
            with self._lock:
                syms = list(self._history_buffers.keys())

            for sym in syms:
                with self._lock:
                    buf = self._history_buffers.pop(sym, [])
                if buf:
                    self._do_flush(sym, buf)

        # Shutdown executor
        if self._flush_executor:
            self._flush_executor.shutdown(wait=True)

        # Stop stream
        if self._stream:
            self._stream.stop()

        logger.info("OrderBookManager stopped")


# ============================================================
# EXPORTS
# ============================================================

__all__ = [
    # Core Classes
    'OrderBookSnapshot',
    'OrderBookLevel',
    'OrderBookAnalyzer',
    'OrderBookManager',

    # Stream Classes
    'BinanceOrderBookStream',
    'BinanceMultiStream',
    'CCXTOrderBookStream',
    'BaseOrderBookStream',

    # Backtest Support
    'BacktestOrderBookProvider',
    'SyntheticOrderBook',

    # Order Flow Analysis
    'OrderFlowMetrics',
    'OrderFlowTracker',

    # Execution Simulation
    'SlippageEstimator',
    'BacktestExecutionSimulator',

    # Support Classes
    'StreamHealth',
    'TradeSide',
    'AlertLevel',
    'DataValidator',
    'AlertManager',
    'StateManager',
    'ThrottledCallback',
    'OrderBookDeltaHandler',
    'TickClassifier',

    # Constants
    'OrderBookConstants',
    'ALL_TRADING_SYMBOLS',

    # Dependency Flags
    'HAS_WEBSOCKETS',
    'HAS_CCXT_PRO',

    # Utility Functions
    '_now_ms',
]


# ============================================================
# TEST UTILITIES
# ============================================================

class TestRunner:
    """Test utilities for running and reporting tests."""

    @staticmethod
    def run_test(name: str, test_fn: Callable[[], bool]) -> bool:
        """Run a single test with error handling."""
        try:
            result = test_fn()
            status = '✅ PASSED' if result else '❌ FAILED'
            print(f"   {status}")
            return result
        except Exception as e:
            print(f"   ❌ ERROR: {e}")
            return False

    @staticmethod
    def timed_loop(
        duration: int,
        interval: float,
        callback: Callable[[float], None]
    ) -> None:
        """Run a timed loop with periodic callbacks."""
        start = time.time()
        try:
            while time.time() - start < duration:
                callback(time.time() - start)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nInterrupted.")

    @staticmethod
    def print_header(title: str) -> None:
        """Print a section header."""
        print("\n" + "=" * 70)
        print(f" {title}")
        print("=" * 70)

    @staticmethod
    def print_summary(title: str, stats: Dict[str, Any]) -> None:
        """Print a summary section."""
        print("\n" + "-" * 70)
        print(f" {title}")
        print("-" * 70)
        for k, v in stats.items():
            print(f"   {k}: {v}")
        print("-" * 70)


# ============================================================
# UNIT TESTS
# ============================================================

def test_order_book() -> bool:
    """Run all unit tests."""
    TestRunner.print_header("ORDER BOOK UNIT TESTS v4.2")

    tests = [
        ("OrderBookLevel", _test_level),
        ("OrderBookSnapshot Basic", _test_snapshot_basic),
        ("OrderBookSnapshot Metrics", _test_snapshot_metrics),
        ("Market Impact", _test_market_impact),
        ("Wall Detection", _test_walls),
        ("OrderBookAnalyzer", _test_analyzer),
        ("Analyzer Time Lookup", _test_analyzer_time_lookup),
        ("Sequence Gaps", _test_sequence_gaps),
        ("TickClassifier", _test_tick_classifier),
        ("OrderFlowTracker", _test_order_flow_tracker),
        ("SlippageEstimator", _test_slippage_estimator),
        ("BacktestExecutionSimulator", _test_execution_simulator),
        ("SyntheticOrderBook", _test_synthetic),
        ("DataValidator", _test_validator),
        ("AlertManager", _test_alerts),
        ("StateManager", _test_state_manager),
        ("ThrottledCallback", _test_throttled),
        ("DeltaHandler", _test_delta_handler),
        ("StreamHealth", _test_stream_health),
        ("BacktestProvider", _test_backtest_provider),
        ("Symbol Validation", _test_symbol_validation),
        ("Edge Cases", _test_edge_cases),
    ]

    passed = 0
    failed = 0

    for name, fn in tests:
        print(f"\n[TEST] {name}")
        if TestRunner.run_test(name, fn):
            passed += 1
        else:
            failed += 1

    print(f"\n{'=' * 70}")
    print(f" RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)

    return failed == 0


def _test_level() -> bool:
    """Test OrderBookLevel creation and validation."""
    # Basic creation
    level = OrderBookLevel(100.0, 10.0)
    assert level.price == 100.0, f"Expected price 100.0, got {level.price}"
    assert level.size == 10.0, f"Expected size 10.0, got {level.size}"
    assert level.value == 1000.0, f"Expected value 1000.0, got {level.value}"

    # String conversion
    level2 = OrderBookLevel("99.5", "5.5")
    assert level2.price == 99.5, f"Expected price 99.5, got {level2.price}"

    # Tuple export
    tup = level.to_tuple()
    assert tup == (100.0, 10.0), f"Expected (100.0, 10.0), got {tup}"

    # Invalid price
    try:
        OrderBookLevel(-1, 10)
        return False  # Should have raised
    except ValueError:
        pass

    # Invalid size
    try:
        OrderBookLevel(100, -1)
        return False  # Should have raised
    except ValueError:
        pass

    return True


def _test_snapshot_basic() -> bool:
    """Test OrderBookSnapshot creation and basic properties."""
    snap = OrderBookSnapshot.from_raw(
        "BTC/USDT",
        [(100, 10), (99, 20)],
        [(101, 15), (102, 25)]
    )

    assert snap.is_valid, "Snapshot should be valid"
    assert not snap.is_crossed, "Snapshot should not be crossed"
    assert snap.best_bid == 100, f"Expected best_bid 100, got {snap.best_bid}"
    assert snap.best_ask == 101, f"Expected best_ask 101, got {snap.best_ask}"

    # Test serialization
    data = snap.to_dict()
    snap2 = OrderBookSnapshot.from_dict(data)
    assert snap2.best_bid == snap.best_bid, "Deserialized bid mismatch"
    assert snap2.best_ask == snap.best_ask, "Deserialized ask mismatch"

    # Test from_sorted (faster path)
    snap3 = OrderBookSnapshot.from_sorted(
        "TEST/USDT",
        [OrderBookLevel(100, 10)],
        [OrderBookLevel(101, 15)]
    )
    assert snap3.is_valid, "from_sorted snapshot should be valid"

    return True


def _test_snapshot_metrics() -> bool:
    """Test OrderBookSnapshot metrics calculations."""
    snap = OrderBookSnapshot.from_raw(
        "TEST/USDT",
        [(100, 50), (99, 30)],
        [(101, 25), (102, 35)]
    )

    # Mid price
    assert snap.mid_price == 100.5, f"Expected mid 100.5, got {snap.mid_price}"

    # Spread
    assert snap.spread == 1.0, f"Expected spread 1.0, got {snap.spread}"

    # Imbalance: (50 - 25) / (50 + 25) = 0.333...
    expected_imb = (50 - 25) / (50 + 25)
    assert abs(snap.imbalance - expected_imb) < 0.01, \
        f"Expected imbalance ~{expected_imb:.3f}, got {snap.imbalance}"

    # Microprice
    mp = snap.microprice()
    assert mp > 0, f"Microprice should be positive, got {mp}"

    # VWAP
    vwap_bid = snap.vwap_bid(2)
    assert vwap_bid > 0, f"VWAP bid should be positive, got {vwap_bid}"

    # Depth imbalance
    depth_imb = snap.depth_imbalance(2)
    assert -1 <= depth_imb <= 1, f"Depth imbalance out of range: {depth_imb}"

    return True


def _test_market_impact() -> bool:
    """Test market impact calculation."""
    snap = OrderBookSnapshot.from_raw(
        "TEST/USDT",
        [(100, 10), (99, 10), (98, 10)],
        [(101, 10), (102, 10), (103, 10)]
    )

    # Small order - fills at best ask
    impact_small = snap.market_impact(5, "buy")
    assert impact_small == 101.0, \
        f"Expected 101.0 for small buy, got {impact_small}"

    # Larger order - walks the book
    # 10 @ 101 + 5 @ 102 = 1010 + 510 = 1520, avg = 1520/15 = 101.33
    impact_large = snap.market_impact(15, "buy")
    expected = (10 * 101 + 5 * 102) / 15
    assert abs(impact_large - expected) < 0.01, \
        f"Expected ~{expected:.2f}, got {impact_large}"

    # Sell side
    impact_sell = snap.market_impact(5, "sell")
    assert impact_sell == 100.0, \
        f"Expected 100.0 for small sell, got {impact_sell}"

    return True


def _test_walls() -> bool:
    """Test wall detection."""
    snap = OrderBookSnapshot.from_raw(
        "TEST/USDT",
        [(100, 10), (99, 150), (98, 10)],   # Wall at 99
        [(101, 10), (102, 200), (103, 10)]  # Wall at 102
    )

    # With threshold 2.5: avg_bid = 56.67, threshold = 141.67
    # 150 >= 141.67 ✓
    walls = snap.find_walls(threshold_mult=2.5)

    assert len(walls["bid_walls"]) >= 1, \
        f"Expected bid wall, got {walls['bid_walls']}"
    assert any(w.price == 99 for w in walls["bid_walls"]), \
        f"Expected wall at 99, got {[w.price for w in walls['bid_walls']]}"

    assert len(walls["ask_walls"]) >= 1, \
        f"Expected ask wall, got {walls['ask_walls']}"
    assert any(w.price == 102 for w in walls["ask_walls"]), \
        f"Expected wall at 102, got {[w.price for w in walls['ask_walls']]}"

    # Test nearest wall functions
    nearest_bid = snap.nearest_bid_wall(threshold_mult=2.5)
    assert nearest_bid is not None, "Expected nearest bid wall"

    nearest_ask = snap.nearest_ask_wall(threshold_mult=2.5)
    assert nearest_ask is not None, "Expected nearest ask wall"

    return True


def _test_analyzer() -> bool:
    """Test OrderBookAnalyzer functionality."""
    analyzer = OrderBookAnalyzer(max_snapshots=50)
    base_time = _now_ms()

    # Add snapshots with increasing bid size (bullish trend)
    for i in range(20):
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10 + i * 2)],  # Bid size increases
            [(101, 10)],
            timestamp=base_time + i * 100,
            sequence=i + 1
        )
        analyzer.add_snapshot(snap)

    assert analyzer.count == 20, f"Expected 20 snapshots, got {analyzer.count}"
    assert analyzer.total_updates == 20, \
        f"Expected 20 updates, got {analyzer.total_updates}"

    # Check trend (should be positive/bullish)
    trend = analyzer.imbalance_trend()
    assert trend > 0, f"Expected positive trend, got {trend}"

    # Should detect bullish condition
    assert analyzer.is_bullish(), "Expected bullish condition"

    # Statistics should be cached
    stats1 = analyzer.get_statistics()
    stats2 = analyzer.get_statistics()
    assert "signal_strength" in stats1, "Expected signal_strength in stats"
    assert stats1 == stats2, "Cached stats should be equal"

    return True


def _test_analyzer_time_lookup() -> bool:
    """Test OrderBookAnalyzer time-based lookup."""
    analyzer = OrderBookAnalyzer(max_snapshots=100)

    # Add snapshots at known times
    times = [1000, 2000, 3000, 4000, 5000]
    for t in times:
        snap = OrderBookSnapshot.from_raw(
            "TEST/USDT",
            [(100, 10)],
            [(101, 10)],
            timestamp=t
        )
        analyzer.add_snapshot(snap)

    # Exact match
    result = analyzer.get_snapshot_at_time(3000)
    assert result is not None, "Expected snapshot at 3000"
    assert result.timestamp == 3000, f"Expected timestamp 3000, got {result.timestamp}"

    # Before first
    result = analyzer.get_snapshot_at_time(500)
    assert result is None, "Expected None before first snapshot"

    # Between snapshots
    result = analyzer.get_snapshot_at_time(2500)
    assert result is not None, "Expected snapshot at or before 2500"
    assert result.timestamp == 2000, f"Expected timestamp 2000, got {result.timestamp}"

    # After last
    result = analyzer.get_snapshot_at_time(6000)
    assert result is not None, "Expected snapshot at or before 6000"
    assert result.timestamp == 5000, f"Expected timestamp 5000, got {result.timestamp}"

    return True


def _test_sequence_gaps() -> bool:
    """Test sequence gap detection."""
    analyzer = OrderBookAnalyzer()

    # Add snapshots with gaps: 1, 2, 5, 6, 10
    # Gaps: 3-4 (2 missing), 7-9 (3 missing) = 5 total
    for seq in [1, 2, 5, 6, 10]:
        snap = OrderBookSnapshot.from_raw(
            "T/U",
            [(100, 10)],
            [(101, 10)],
            sequence=seq
        )
        analyzer.add_snapshot(snap)

    assert analyzer.sequence_gaps == 5, \
        f"Expected 5 sequence gaps, got {analyzer.sequence_gaps}"

    return True


def _test_tick_classifier() -> bool:
    """Test tick classification."""
    classifier = TickClassifier()

    prices = [100.0, 100.1, 100.2, 100.2, 100.1]
    results = classifier.classify_batch(prices)

    expected = [
        TradeSide.UNKNOWN,  # First tick
        TradeSide.BUY,      # Uptick
        TradeSide.BUY,      # Uptick
        TradeSide.BUY,      # Same price, keep last (BUY)
        TradeSide.SELL,     # Downtick
    ]

    assert results == expected, f"Expected {expected}, got {results}"

    # Test reset
    classifier.reset()
    result = classifier.classify(100.0)
    assert result == TradeSide.UNKNOWN, "After reset, first tick should be UNKNOWN"

    return True


def _test_order_flow_tracker() -> bool:
    """Test OrderFlowTracker functionality."""
    tracker = OrderFlowTracker(large_trade_threshold=5.0)

    # Process trades
    tracker.process_trade(100.0, 1.0)   # Unknown (first)
    tracker.process_trade(100.1, 2.0)   # Buy (uptick)
    tracker.process_trade(100.2, 3.0)   # Buy (uptick)
    tracker.process_trade(100.1, 1.5)   # Sell (downtick)
    tracker.process_trade(100.0, 10.0)  # Sell (downtick, large)

    metrics = tracker.get_metrics()

    # Verify counts
    assert metrics.trade_count == 4, \
        f"Expected 4 classified trades, got {metrics.trade_count}"
    assert metrics.unknown_count == 1, \
        f"Expected 1 unknown, got {metrics.unknown_count}"
    assert metrics.total_trade_count == 5, \
        f"Expected 5 total trades, got {metrics.total_trade_count}"

    # Verify volumes
    assert metrics.buy_volume > 0, \
        f"Expected buy_volume > 0, got {metrics.buy_volume}"
    assert metrics.sell_volume > 0, \
        f"Expected sell_volume > 0, got {metrics.sell_volume}"

    # Verify large trade count
    assert metrics.large_sell_count == 1, \
        f"Expected 1 large sell, got {metrics.large_sell_count}"

    # Verify OFI in range
    ofi = metrics.ofi
    assert -1.0 <= ofi <= 1.0, f"OFI out of range: {ofi}"

    # Verify classification rate
    rate = metrics.classification_rate
    assert rate == 0.8, f"Expected 80% classification rate, got {rate}"

    # Test batch processing
    tracker.reset()
    trades = [(100.0, 1.0), (100.1, 2.0), (99.9, 3.0)]
    batch_metrics = tracker.process_batch(trades)
    assert batch_metrics.total_trade_count == 3, \
        f"Expected 3 trades after batch, got {batch_metrics.total_trade_count}"

    return True


def _test_slippage_estimator() -> bool:
    """Test SlippageEstimator functionality."""
    snap = OrderBookSnapshot.from_raw(
        "TEST/USDT",
        [(100, 10), (99, 20), (98, 30)],
        [(101, 10), (102, 20), (103, 30)]
    )

    # Test estimate
    price, slippage, fill_prob = SlippageEstimator.estimate(
        snap, 5, "buy", aggression=1.0
    )
    assert price > 0, f"Expected price > 0, got {price}"
    assert slippage >= 0, f"Expected slippage >= 0, got {slippage}"
    assert 0 <= fill_prob <= 1, f"Fill probability out of range: {fill_prob}"

    # Test fixed slippage
    fixed_buy = SlippageEstimator.estimate_fixed(100.0, "buy", 5.0)
    assert fixed_buy > 100.0, f"Expected buy price > 100, got {fixed_buy}"
    assert abs(fixed_buy - 100.05) < 0.001, \
        f"Expected ~100.05, got {fixed_buy}"

    fixed_sell = SlippageEstimator.estimate_fixed(100.0, "sell", 5.0)
    assert fixed_sell < 100.0, f"Expected sell price < 100, got {fixed_sell}"

    # Test simulate_fill
    result = SlippageEstimator.simulate_fill(snap, 5, "buy", max_slippage_bps=50.0)
    assert result is not None, "Expected successful fill"
    fill_price, filled_size = result
    assert fill_price > 0, f"Expected fill_price > 0, got {fill_price}"
    assert filled_size > 0, f"Expected filled_size > 0, got {filled_size}"

    # Test with insufficient slippage limit
    result = SlippageEstimator.simulate_fill(snap, 100, "buy", max_slippage_bps=0.1)
    # Should fail or partially fill due to tight limit

    # Test market impact calculation
    impact = SlippageEstimator.calculate_market_impact_cost(snap, 15, "buy")
    assert "slippage_bps" in impact, "Expected slippage_bps in impact"
    assert impact["slippage_bps"] >= 0, "Slippage should be non-negative"

    return True


def _test_execution_simulator() -> bool:
    """Test BacktestExecutionSimulator functionality."""
    snap = OrderBookSnapshot.from_raw(
        "TEST/USDT",
        [(100, 10), (99, 20)],
        [(101, 10), (102, 20)]
    )

    # Test with fixed slippage model
    sim_fixed = BacktestExecutionSimulator(
        slippage_model="fixed",
        fixed_slippage_bps=2.0,
        fee_bps=4.0
    )

    entry_fixed = sim_fixed.simulate_entry(100.5, 1.0, "buy")
    assert entry_fixed["fill_price"] > 100.5, \
        f"Expected slippage on buy, got {entry_fixed['fill_price']}"
    assert entry_fixed["fee"] > 0, \
        f"Expected fee > 0, got {entry_fixed['fee']}"
    assert entry_fixed["total_outlay"] > 0, \
        f"Expected total_outlay > 0 for buy"

    # Test with realistic model
    sim_real = BacktestExecutionSimulator(
        slippage_model="realistic",
        fee_bps=4.0
    )
    entry_real = sim_real.simulate_entry(100.5, 1.0, "buy", orderbook=snap)
    assert entry_real["fill_price"] > 0, "Expected valid fill price"

    # Test round trip
    result = sim_fixed.simulate_round_trip(100.5, 105.0, 1.0, "buy")
    assert "net_pnl" in result, "Expected net_pnl in result"
    assert "return_pct" in result, "Expected return_pct in result"
    assert result["gross_pnl"] > 0, \
        f"Expected profit on 100.5 -> 105, got {result['gross_pnl']}"

    # Test sell side
    entry_sell = sim_fixed.simulate_entry(100.5, 1.0, "sell")
    assert entry_sell["net_proceeds"] > 0, \
        "Expected net_proceeds > 0 for sell"

    # Test statistics
    stats = sim_fixed.get_statistics()
    assert stats["trade_count"] >= 2, \
        f"Expected at least 2 trades, got {stats['trade_count']}"

    # Test invalid inputs
    try:
        BacktestExecutionSimulator(slippage_model="invalid")
        return False  # Should have raised
    except ValueError:
        pass

    return True


def _test_synthetic() -> bool:
    """Test SyntheticOrderBook generation."""
    ticks = [(i * 1000, 100.0 + (i % 10) * 0.01) for i in range(100)]

    # Test from_ticks with reproducibility
    snap1 = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)
    snap2 = SyntheticOrderBook.from_ticks(ticks, spread_bps=10, random_seed=42)

    assert snap1 is not None, "Expected snapshot from ticks"
    assert snap2 is not None, "Expected snapshot from ticks"
    assert snap1.best_bid == snap2.best_bid, "Same seed should produce same results"

    # Test from_candle
    snap_candle = SyntheticOrderBook.from_candle(
        timestamp=1000,
        open_price=100.0,
        high_price=101.0,
        low_price=99.0,
        close_price=100.5,
        volume=1000,
        random_seed=42
    )
    assert snap_candle.is_valid, "Candle snapshot should be valid"

    # Test generate_analyzer
    analyzer = SyntheticOrderBook.generate_analyzer(ticks, sample_every=10)
    assert analyzer is not None, "Expected analyzer from ticks"
    assert analyzer.count == 10, f"Expected 10 snapshots, got {analyzer.count}"

    # Test with tick sizes
    tick_sizes = [random.uniform(0.1, 2.0) for _ in range(100)]
    snap_with_sizes = SyntheticOrderBook.from_ticks(
        ticks,
        tick_sizes=tick_sizes,
        random_seed=42
    )
    assert snap_with_sizes is not None, "Expected snapshot with tick sizes"

    return True


def _test_validator() -> bool:
    """Test DataValidator functionality."""
    validator = DataValidator(max_symbols=10)

    # Valid snapshot
    valid_snap = OrderBookSnapshot.from_raw(
        "T/U",
        [(100, 10)],
        [(101, 10)]
    )
    is_valid, warnings = validator.validate(valid_snap)
    assert is_valid, "Expected valid snapshot to pass"

    # Crossed book
    crossed_snap = OrderBookSnapshot.from_raw(
        "T/U",
        [(102, 10)],
        [(101, 10)]
    )
    is_valid, warnings = validator.validate(crossed_snap)
    assert not is_valid, "Expected crossed book to fail"

    # Empty book
    empty_snap = OrderBookSnapshot.from_raw("T/U", [], [])
    is_valid, warnings = validator.validate(empty_snap)
    assert not is_valid, "Expected empty book to fail"

    # Test reset
    validator.reset("T/U")
    stats = validator.get_stats()
    assert stats["tracked_symbols"] == 0, "Expected 0 symbols after reset"

    return True


def _test_alerts() -> bool:
    """Test AlertManager functionality."""
    alerts = AlertManager(throttle_seconds=1)
    received: List[Tuple[str, str]] = []

    def handler(level: str, message: str, context: Dict[str, Any]) -> None:
        received.append((level, message))

    alerts.add_handler(handler)

    # First alert should go through
    r1 = alerts.warning("Test", throttle_key="t1")
    assert r1, "First alert should not be throttled"

    # Critical without throttle key
    alerts.critical("Critical")

    # Same throttle key should be throttled
    r2 = alerts.warning("Test", throttle_key="t1")
    assert not r2, "Second alert with same key should be throttled"

    # Verify received
    assert len(received) == 2, f"Expected 2 alerts, got {len(received)}"

    # Test clear throttle
    alerts.clear_throttle()
    r3 = alerts.warning("Test", throttle_key="t1")
    assert r3, "Alert after clear should not be throttled"

    return True


def _test_state_manager() -> bool:
    """Test StateManager functionality."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        sf = os.path.join(tmpdir, "test.json")

        # Create and save state
        s1 = StateManager(sf)
        s1.set("key", "val")
        s1.update_positions({"BTC/USDT": 0.5})

        # Load state in new instance
        s2 = StateManager(sf)
        assert s2.get("key") == "val", "Expected persisted value"

        positions = s2.get_positions()
        assert positions.get("BTC/USDT") == 0.5, "Expected persisted position"

        # Test running state
        s2.set_running(True)
        assert s2.was_running(), "Expected running state"

        # Test clear
        s2.clear()
        assert s2.get("key") is None, "Expected cleared state"

    return True


def _test_throttled() -> bool:
    """Test ThrottledCallback functionality."""
    count = [0]

    def callback(data: Any) -> None:
        count[0] += 1

    tc = ThrottledCallback(callback, min_interval_ms=100)

    # Rapid calls should be throttled
    for i in range(10):
        tc(i)

    assert count[0] == 1, f"Expected 1 call after rapid fire, got {count[0]}"

    # Wait and call again
    time.sleep(0.15)
    tc(99)

    assert count[0] == 2, f"Expected 2 calls after delay, got {count[0]}"

    # Test flush
    tc(100)  # This gets stored as pending
    tc.flush()

    assert count[0] == 3, f"Expected 3 calls after flush, got {count[0]}"

    return True


def _test_delta_handler() -> bool:
    """Test OrderBookDeltaHandler functionality."""
    handler = OrderBookDeltaHandler("T/U")

    # Apply snapshot
    handler.apply_snapshot(
        [(100, 10), (99, 20)],
        [(101, 15), (102, 25)],
        update_id=1
    )

    s1 = handler.get_snapshot()
    assert s1 is not None, "Expected snapshot after apply"
    assert s1.best_bid == 100, f"Expected best_bid 100, got {s1.best_bid}"

    # Apply delta - remove price 100, add price 98
    s2 = handler.apply_delta(
        [(100, 0), (98, 30)],  # Remove 100, add 98
        [(101, 20)],          # Update 101
        update_id=2
    )

    assert s2 is not None, "Expected snapshot after delta"
    assert s2.best_bid == 99, f"Expected best_bid 99, got {s2.best_bid}"

    # Stale update should return None
    s3 = handler.apply_delta([(97, 10)], [], update_id=1)
    assert s3 is None, "Expected None for stale update"

    # Test reset
    handler.reset()
    s4 = handler.get_snapshot()
    assert s4 is None, "Expected None after reset"

    return True


def _test_stream_health() -> bool:
    """Test StreamHealth functionality."""
    h = StreamHealth()

    # Initially unhealthy
    assert not h.is_healthy, "New health should not be healthy"

    # Make healthy
    h.connected = True
    h.last_update_ms = _now_ms()
    h.latency_ms = 50

    assert h.is_healthy, "Should be healthy now"

    # Record errors
    for _ in range(15):
        h.record_error("test error")

    assert not h.is_healthy, "Should be unhealthy after many errors"
    assert h.error_count == 15, f"Expected 15 errors, got {h.error_count}"

    # Reset errors
    h.reset_errors()
    assert h.is_healthy, "Should be healthy after reset"

    # Test to_dict
    d = h.to_dict()
    assert "connected" in d, "Expected connected in dict"
    assert "is_healthy" in d, "Expected is_healthy in dict"

    return True


def _test_backtest_provider() -> bool:
    """Test BacktestOrderBookProvider functionality."""
    provider = BacktestOrderBookProvider()

    # Load candles
    candles = [
        {
            "timestamp": i * 60000,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000
        }
        for i in range(10)
    ]
    provider.load_from_candles("BTC/USDT", candles)

    # Check analyzer
    analyzer = provider.get_analyzer("BTC/USDT")
    assert analyzer is not None, "Expected analyzer"
    assert analyzer.count == 10, f"Expected 10 snapshots, got {analyzer.count}"

    # Check snapshot
    snap = provider.get_snapshot("BTC/USDT")
    assert snap is not None, "Expected snapshot"

    # Check time-based lookup
    snap_at_time = provider.get_snapshot_at_time("BTC/USDT", 5 * 60000)
    assert snap_at_time is not None, "Expected snapshot at time"
    assert snap_at_time.timestamp <= 5 * 60000, "Snapshot should be at or before time"

    # Check symbols list
    symbols = provider.get_all_symbols()
    assert "BTC/USDT" in symbols, "Expected BTC/USDT in symbols"

    # Test clear
    provider.clear("BTC/USDT")
    assert provider.get_analyzer("BTC/USDT") is None, "Expected None after clear"

    return True


def _test_symbol_validation() -> bool:
    """Test symbol validation pattern."""
    valid = ["BTC/USDT", "ETH/BTC", "1INCH/USDT", "XRP/USDT"]
    invalid = ["btc/usdt", "BTC-USDT", "BTC", "BTC/", "/USDT"]

    for s in valid:
        if not OrderBookConstants.SYMBOL_PATTERN.match(s):
            print(f"   Expected {s} to be valid")
            return False

    for s in invalid:
        if OrderBookConstants.SYMBOL_PATTERN.match(s):
            print(f"   Expected {s} to be invalid")
            return False

    return True


def _test_edge_cases() -> bool:
    """Test edge cases and error handling."""
    # Empty book
    empty = OrderBookSnapshot.from_raw("T/U", [], [])
    assert not empty.is_valid, "Empty book should be invalid"
    assert empty.mid_price == 0.0, "Empty book mid should be 0"
    assert empty.spread == 0.0, "Empty book spread should be 0"

    # Crossed book
    crossed = OrderBookSnapshot.from_raw("T/U", [(102, 10)], [(101, 10)])
    assert crossed.is_crossed, "Should detect crossed book"

    # Stale data
    old_ts = _now_ms() - 10000
    stale = OrderBookSnapshot.from_raw(
        "T/U",
        [(100, 10)],
        [(101, 10)],
        timestamp=old_ts
    )
    assert stale.is_stale(5000), "Should detect stale data"

    # Market impact with zero size
    snap = OrderBookSnapshot.from_raw("T/U", [(100, 10)], [(101, 10)])
    impact = snap.market_impact(0, "buy")
    assert impact == snap.mid_price, "Zero size should return mid price"

    # Market impact with empty book
    impact_empty = empty.market_impact(1, "buy")
    assert impact_empty == 0.0, "Empty book impact should be 0"

    return True


# ============================================================
# LIVE TESTS
# ============================================================

def test_live_stream(duration: int = 30) -> bool:
    """Test live WebSocket streaming."""
    if not HAS_WEBSOCKETS:
        print("❌ websockets not installed")
        return False

    TestRunner.print_header(f"LIVE STREAM TEST ({duration}s)")

    manager = OrderBookManager(store_history=False, enable_alerts=True)
    stats: Dict[str, Any] = {"count": 0, "spreads": [], "imbalances": []}

    def on_update(s: OrderBookSnapshot) -> None:
        stats["count"] += 1
        if s.is_valid:
            stats["spreads"].append(s.spread)
            stats["imbalances"].append(s.imbalance)

        if stats["count"] % 10 == 0:
            h = manager.get_health("BTC/USDT")
            if s.is_valid and h:
                print(
                    f"[{stats['count']:4d}] "
                    f"Mid: ${s.mid_price:,.2f} | "
                    f"Spread: {s.spread_bps:.3f}bps | "
                    f"Imb: {s.imbalance:+.3f} | "
                    f"Lat: {h.latency_ms:.0f}ms"
                )

    manager.subscribe("BTC/USDT", on_update)
    TestRunner.timed_loop(duration, 1, lambda _: None)
    health = manager.get_health("BTC/USDT")
    manager.stop()

    TestRunner.print_summary("LIVE TEST SUMMARY", {
        "Updates": f"{stats['count']} ({stats['count']/max(1,duration):.1f}/sec)",
        "Spread range": (
            f"${min(stats['spreads']):.2f} - ${max(stats['spreads']):.2f}"
            if stats['spreads'] else "N/A"
        ),
        "Avg imbalance": (
            f"{sum(stats['imbalances'])/len(stats['imbalances']):+.3f}"
            if stats['imbalances'] else "N/A"
        ),
        "Health": "✅" if health and health.is_healthy else "⚠️",
        "Latency": f"{health.latency_ms:.1f}ms" if health else "N/A",
    })

    success = stats["count"] > 0
    print(f" {'✅ PASSED' if success else '❌ FAILED'}")
    return success


def test_multi_symbol(duration: int = 30, num_pairs: int = 10) -> bool:
    """Test multi-symbol WebSocket streaming."""
    if not HAS_WEBSOCKETS:
        print("❌ websockets not installed")
        return False

    TestRunner.print_header(f"MULTI-SYMBOL TEST ({num_pairs} pairs, {duration}s)")

    symbols = ALL_TRADING_SYMBOLS[:num_pairs]
    manager = OrderBookManager(multi_symbol=True, store_history=False)
    counts: Dict[str, int] = {s: 0 for s in symbols}

    def on_update(s: OrderBookSnapshot) -> None:
        counts[s.symbol] = counts.get(s.symbol, 0) + 1

    manager.subscribe_many(symbols, on_update)

    def report(elapsed: float) -> None:
        h = manager.get_health_report()
        total = sum(counts.values())
        active = sum(1 for c in counts.values() if c > 0)
        print(
            f"   [{elapsed:5.0f}s] "
            f"Updates: {total:5d} ({total/max(1,elapsed):6.1f}/s) | "
            f"Active: {active}/{len(symbols)} | "
            f"Healthy: {h.get('healthy_count', 0)}"
        )

    TestRunner.timed_loop(duration, 5, report)
    manager.stop()

    total = sum(counts.values())
    TestRunner.print_summary("MULTI-SYMBOL SUMMARY", {
        "Symbols": len(symbols),
        "Total updates": total,
        "Rate": f"{total/max(1,duration):.1f}/sec",
        "Top 5": ", ".join(
            f"{s}:{c}" for s, c in
            sorted(counts.items(), key=lambda x: -x[1])[:5]
        ),
    })

    success = total > len(symbols) * 3
    print(f" {'✅ PASSED' if success else '❌ FAILED'}")
    return success


def test_stress(duration: int = 60) -> bool:
    """Stress test with maximum symbols."""
    if not HAS_WEBSOCKETS:
        print("❌ websockets not installed")
        return False

    TestRunner.print_header(f"STRESS TEST ({duration}s)")

    manager = OrderBookManager(multi_symbol=True, store_history=False)
    max_symbols = min(len(ALL_TRADING_SYMBOLS), manager._stream.max_capacity)
    symbols = ALL_TRADING_SYMBOLS[:max_symbols]
    counts: Dict[str, int] = {s: 0 for s in symbols}

    print(f" Capacity: {manager._stream.max_capacity}, Testing: {len(symbols)} symbols")

    def on_update(s: OrderBookSnapshot) -> None:
        counts[s.symbol] = counts.get(s.symbol, 0) + 1

    manager.subscribe_many(symbols, on_update)

    def report(elapsed: float) -> None:
        active = sum(1 for c in counts.values() if c > 0)
        total = sum(counts.values())
        print(
            f"   [{elapsed:5.0f}s] "
            f"Updates: {total:6d} ({total/max(1,elapsed):6.1f}/s) | "
            f"Active: {active}/{len(symbols)}"
        )

    TestRunner.timed_loop(duration, 10, report)
    manager.stop()

    active = sum(1 for c in counts.values() if c > 0)
    dead = [s for s, c in counts.items() if c == 0]

    if dead:
        print(f"\n   ⚠️ Dead symbols ({len(dead)}): {dead[:10]}")

    TestRunner.print_summary("STRESS SUMMARY", {
        "Symbols": len(symbols),
        "Active": f"{active} ({100*active/len(symbols):.1f}%)",
        "Updates": sum(counts.values()),
    })

    success = active >= len(symbols) * 0.9
    print(f" {'✅ PASSED' if success else '❌ FAILED'}")
    return success


def test_backtest() -> bool:
    """Test backtest mode functionality."""
    TestRunner.print_header("BACKTEST TEST")
    random.seed(42)

    manager = OrderBookManager(backtest_mode=True)

    # Generate test candles
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

    # Verify data loaded
    analyzer = manager.get_analyzer("BTC/USDT")
    if not analyzer or analyzer.count != 100:
        print(f"   ❌ Expected 100 snapshots, got {analyzer.count if analyzer else 0}")
        return False

    snapshot = manager.get_snapshot("BTC/USDT")
    if not snapshot or not snapshot.is_valid:
        print("   ❌ Invalid snapshot")
        return False

    # Test time-based lookup
    snap_at_time = manager.get_backtest_snapshot("BTC/USDT", 50 * 60000)
    if not snap_at_time:
        print("   ❌ Time-based lookup failed")
        return False

    stats = analyzer.get_statistics()
    print(f"   Mid price: ${stats.get('mid_price', 0):,.2f}")
    print(f"   Spread: {stats.get('spread_bps', 0):.2f} bps")
    print(f"   Signal: {stats.get('signal_strength', 0):+.3f}")

    manager.stop()
    print(" ✅ PASSED")
    return True


def test_execution_integration() -> bool:
    """Test execution simulator integration with backtest."""
    TestRunner.print_header("EXECUTION INTEGRATION TEST")
    random.seed(42)

    # Create backtest manager
    manager = OrderBookManager(backtest_mode=True)

    # Generate trending candles
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

    # Create execution simulator
    simulator = BacktestExecutionSimulator(
        slippage_model="realistic",
        fee_bps=4.0
    )

    # Simulate trades at different points
    results = []
    for i in range(10, 50, 5):
        ob = manager.get_backtest_snapshot("BTC/USDT", i * 60000)
        if ob and ob.is_valid:
            entry_price = ob.mid_price
            exit_price = entry_price * 1.002  # 0.2% profit target

            result = simulator.simulate_round_trip(
                entry_price,
                exit_price,
                0.01,
                "buy",
                entry_orderbook=ob
            )
            results.append(result)

    total_pnl = sum(r["net_pnl"] for r in results)
    wins = sum(1 for r in results if r["profitable"])

    print(f"   Trades: {len(results)}")
    print(f"   Wins: {wins}/{len(results)}")
    print(f"   Total PnL: ${total_pnl:.4f}")

    stats = simulator.get_statistics()
    print(f"   Total Fees: ${stats['total_fees']:.6f}")
    print(f"   Total Slippage: ${stats['total_slippage_cost']:.6f}")

    manager.stop()
    print(" ✅ PASSED")
    return True


def show_connection_stats() -> None:
    """Display connection configuration."""
    TestRunner.print_header("CONNECTION CONFIGURATION")

    print("\n WebSocket Settings:")
    print(f"   Ping interval: {OrderBookConstants.WS_PING_INTERVAL}s")
    print(f"   Ping timeout: {OrderBookConstants.WS_PING_TIMEOUT}s")
    print(f"   Recv timeout: {OrderBookConstants.WS_RECV_TIMEOUT}s")
    print(f"   Max message: {OrderBookConstants.WS_MAX_MESSAGE_SIZE / 1024 / 1024:.0f}MB")

    print("\n Reconnection Settings:")
    print(f"   Max attempts: {OrderBookConstants.MAX_RECONNECT_ATTEMPTS}")
    print(f"   Backoff: {OrderBookConstants.INITIAL_BACKOFF}s - {OrderBookConstants.MAX_BACKOFF}s")

    print("\n Multi-Stream Settings:")
    print(
        f"   {BinanceMultiStream.MAX_STREAMS_PER_CONNECTION}/conn × "
        f"{BinanceMultiStream.MAX_CONNECTIONS} = "
        f"{BinanceMultiStream.MAX_STREAMS_PER_CONNECTION * BinanceMultiStream.MAX_CONNECTIONS} pairs"
    )

    print("\n Health Settings:")
    print(f"   Stale threshold: {OrderBookConstants.STALE_DATA_MS}ms")
    print(f"   Spread alert: {OrderBookConstants.MAX_SPREAD_BPS_ALERT}bps")
    print(f"   Price spike: {OrderBookConstants.MAX_PRICE_SPIKE_PCT}%")

    print("\n Execution Defaults:")
    print(f"   Fee: {OrderBookConstants.DEFAULT_FEE_BPS}bps")
    print(f"   Slippage: {OrderBookConstants.DEFAULT_SLIPPAGE_BPS}bps")
    print(f"   Large trade threshold: {OrderBookConstants.LARGE_TRADE_THRESHOLD}")


def test_capacity_limits() -> None:
    """Test symbol capacity limits."""
    TestRunner.print_header("CAPACITY LIMITS")

    stream = BinanceMultiStream()
    print(
        f"\n Config: {stream.MAX_STREAMS_PER_CONNECTION}/conn × "
        f"{stream.MAX_CONNECTIONS} = {stream.max_capacity}"
    )

    for size in [10, 50, 100, 200, 500, 1000]:
        symbols = [f"S{i}/USDT" for i in range(size)]
        chunks = stream._chunk_symbols(symbols)
        actual = sum(len(c) for c in chunks)
        capped = min(size, stream.max_capacity)
        status = '✅' if actual >= capped else f'⚠️ truncated to {actual}'
        print(f"   {size:4d} → {len(chunks)} chunks {status}")


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main() -> None:
    """Main entry point."""
    print(f"\n{'=' * 70}")
    print(" ORACLE ORDER BOOK v4.2 (CORRECTED)")
    print("=" * 70)
    print(f" Python: {sys.version.split()[0]} | WS: {HAS_WEBSOCKETS} | CCXT: {HAS_CCXT_PRO}")

    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if cmd == "unit":
        test_order_book()

    elif cmd == "live":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        test_live_stream(duration)

    elif cmd == "multi":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        num_pairs = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        test_multi_symbol(duration, num_pairs)

    elif cmd == "stress":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        test_stress(duration)

    elif cmd == "backtest":
        test_backtest()

    elif cmd == "execution":
        test_execution_integration()

    elif cmd == "stats":
        show_connection_stats()

    elif cmd == "limits":
        test_capacity_limits()

    elif cmd == "all":
        test_order_book()
        test_backtest()
        test_execution_integration()
        if HAS_WEBSOCKETS:
            test_live_stream(15)
            test_multi_symbol(15, 5)

    elif cmd in ["help", "-h", "--help"]:
        print("""
Usage: python oracle_orderbook.py [command] [args]

Commands:
  unit              Run unit tests
  live [duration]   Live stream test (default 30s)
  multi [dur] [n]   Multi-symbol test (default 30s, 10 pairs)
  stress [duration] Stress test all symbols (default 60s)
  backtest          Backtest mode test
  execution         Execution simulator integration test
  stats             Show connection configuration
  limits            Show capacity limits
  all               Run all tests
  help              Show this help
        """)

    else:
        # Default to unit tests
        test_order_book()


if __name__ == "__main__":
    main()           
            
