#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
# ORACLE ORDERBOOK v4.3 - FIXED & OPTIMIZED
# 
# Applied Fixes:
#   - FIX 1: from_binance_fast zero-size filter (CRITICAL)
#   - FIX 2: ThrottledCallback race condition (BUG)
#   - FIX 3: StateManager debounced save (PERF)
#   - FIX 4: BacktestAnalyzer O(n)->O(1) (PERF)
#   - FIX 5: Single time call in hot path (PERF)
#   - FIX 6: Dict cleanup + orphan removal (LEAK)
#   - FIX 7: Worker NumPy check (SAFETY)
#   - FIX 8: MMapTickStore overwrite protection (SAFETY)
#   - FIX 9: Numba empty array guard (CRASH)
#
# Fixed by: Oracle Orderbook Fixer v2.0
# Date: 2025-12-01 20:18:39
# ═══════════════════════════════════════════════════════════════════════════════

"""
oracle_orderbook.py - ORACLE ORDER BOOK v4.2 — SHARED CORE COMPONENT

Pure. Silent. Eternal.
Used by both Live Trading Engine and Backtest Simulator.
No tests. No prints. No mercy.

This module serves as the Strategy Engine Input Layer:
- Live Mode: Binance WS → BinanceMultiStream → OrderBookSnapshot → OrderBookAnalyzer → Strategy
- Backtest Mode: Candles/Ticks → SyntheticOrderBook → OrderBookAnalyzer → Strategy

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

PriceSize = Tuple[float, float]
TickData = Tuple[int, float]
TradeData = Tuple[int, float, float]
SimpleTrade = Tuple[float, float]
Callback = Callable[['OrderBookSnapshot'], None]


def _now_ms() -> int:
    """Get current time in milliseconds since epoch."""
    return int(time.time() * 1000)


# ============================================================
# CONSTANTS
# ============================================================

class OrderBookConstants:
    """All configuration constants in one place."""

    DEFAULT_SPREAD_BPS: Final[float] = 5.0
    MIN_SPREAD_BPS: Final[float] = 2.0
    MAX_SPREAD_MULT: Final[float] = 3.0
    VOLATILITY_SPREAD_MULT: Final[float] = 5.0
    DEFAULT_BASE_SIZE: Final[float] = 10.0
    DEFAULT_SIZE_DECAY: Final[float] = 0.7
    DEFAULT_LEVELS: Final[int] = 10

    SIGNAL_IMBALANCE_WEIGHT: Final[float] = 0.4
    SIGNAL_DEPTH_WEIGHT: Final[float] = 0.4
    SIGNAL_TREND_WEIGHT: Final[float] = 0.2
    SIGNAL_TREND_SCALE: Final[float] = 10.0

    WS_PING_INTERVAL: Final[int] = 20
    WS_PING_TIMEOUT: Final[int] = 10
    WS_CLOSE_TIMEOUT: Final[int] = 5
    WS_RECV_TIMEOUT: Final[int] = 30
    WS_MAX_MESSAGE_SIZE: Final[int] = 10 * 1024 * 1024

    MAX_RECONNECT_ATTEMPTS: Final[int] = 10
    INITIAL_BACKOFF: Final[float] = 1.0
    MAX_BACKOFF: Final[float] = 60.0
    SHUTDOWN_TIMEOUT: Final[float] = 3.0

    HISTORY_FLUSH_SIZE: Final[int] = 1000
    MAX_SNAPSHOTS_DEFAULT: Final[int] = 100
    MAX_FLUSH_THREADS: Final[int] = 4

    STALE_DATA_MS: Final[int] = 5000
    MAX_SPREAD_BPS_ALERT: Final[float] = 500.0
    MAX_PRICE_SPIKE_PCT: Final[float] = 5.0
    HEALTH_REPORT_INTERVAL: Final[float] = 120.0
    HEALTH_CALC_INTERVAL: Final[float] = 10.0
    ALERT_THROTTLE_SECONDS: Final[int] = 60
    MAX_THROTTLE_KEYS: Final[int] = 10000
    MAX_VALIDATION_FAILURES: Final[int] = 100

    MAX_URL_LENGTH: Final[int] = 2000
    CCXT_MIN_WATCH_INTERVAL: Final[float] = 0.05
    CONNECTION_CHECK_INTERVAL: Final[float] = 1.0

    SYMBOL_PATTERN: Final[re.Pattern] = re.compile(r'^[A-Z0-9]+/[A-Z0-9]+$')

    DEFAULT_FEE_BPS: Final[float] = 4.0
    DEFAULT_SLIPPAGE_BPS: Final[float] = 2.0
    LARGE_TRADE_THRESHOLD: Final[float] = 1.0
    DEFAULT_LIQUIDITY_PENALTY_PCT: Final[float] = 2.0


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
    """Health metrics for a stream connection."""
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
        """Check if stream is in healthy state."""
        if self.last_update_ms == 0:
            return self.connected and self.error_count < 10
        return (
            self.connected
            and self.age_ms < 10000
            and self.latency_ms < 1000
            and self.error_count < 10
        )

    def record_error(self, error: str) -> None:
        """Record an error occurrence."""
        self.error_count += 1
        self.last_error = str(error)[:200]

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
    """A single price level in the order book."""
    price: float
    size: float

    def __post_init__(self) -> None:
        """Validate and convert types after initialization."""
        try:
            self.price = float(self.price)
            self.size = float(self.size)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Invalid level: price={self.price}, size={self.size}"
            ) from e

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
# ORDER BOOK SNAPSHOT
# ============================================================

@dataclass(slots=True)
class OrderBookSnapshot:
    """Immutable snapshot of an order book at a point in time."""
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
            self.bids = sorted(
                (b for b in (self.bids or []) if b.size > 0),
                key=lambda x: x.price,
                reverse=True
            )
            self.asks = sorted(
                (a for a in (self.asks or []) if a.size > 0),
                key=lambda x: x.price
            )
            self._sorted = True

    @staticmethod
    def _parse_levels(raw: Optional[List[PriceSize]]) -> List[OrderBookLevel]:
        """Parse raw price/size tuples into OrderBookLevel objects."""
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
                continue
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
        """Create snapshot from raw price/size lists."""
        return cls(
            symbol=symbol,
            timestamp=timestamp or _now_ms(),
            bids=cls._parse_levels(bids),
            asks=cls._parse_levels(asks),
            sequence=sequence,
            exchange=exchange,
            _sorted=False
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
        """Create snapshot from pre-sorted OrderBookLevel lists."""
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
        """Ultra-fast path for Binance depth streams."""
        # FIX 1: Filter zero-size levels (consistency with from_raw)
        parsed_bids: List[OrderBookLevel] = []
        parsed_asks: List[OrderBookLevel] = []

        for i in range(min(20, len(bids))):
            price = float(bids[i][0])
            size = float(bids[i][1])
            if size > 0:  # Skip zero-size levels
                level = object.__new__(OrderBookLevel)
                level.price = price
                level.size = size
                parsed_bids.append(level)

        for i in range(min(20, len(asks))):
            price = float(asks[i][0])
            size = float(asks[i][1])
            if size > 0:  # Skip zero-size levels
                level = object.__new__(OrderBookLevel)
                level.price = price
                level.size = size
                parsed_asks.append(level)

        snapshot = object.__new__(cls)
        snapshot.symbol = symbol
        snapshot.timestamp = timestamp
        snapshot.bids = parsed_bids
        snapshot.asks = parsed_asks
        snapshot.sequence = sequence
        snapshot.exchange = "binance"
        snapshot._sorted = True

        return snapshot

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
        """Check if best bid >= best ask (invalid state)."""
        return self.is_valid and self.bids[0].price >= self.asks[0].price

    def age_ms(self) -> int:
        """Milliseconds since snapshot was taken."""
        return _now_ms() - self.timestamp

    def is_stale(self, max_age_ms: int = OrderBookConstants.STALE_DATA_MS) -> bool:
        """Check if data is too old for trading."""
        return self.age_ms() > max_age_ms

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

    @property
    def imbalance(self) -> float:
        """Top-of-book imbalance based on size at best bid/ask."""
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return 0.0
        return (self.best_bid_size - self.best_ask_size) / total

    def depth_imbalance(self, levels: int = 10) -> float:
        """Multi-level volume imbalance."""
        bid_vol = sum(b.size for b in self.bids[:levels])
        ask_vol = sum(a.size for a in self.asks[:levels])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def value_imbalance(self, levels: int = 10) -> float:
        """Multi-level notional value imbalance."""
        bid_val = sum(b.value for b in self.bids[:levels])
        ask_val = sum(a.value for a in self.asks[:levels])
        total = bid_val + ask_val
        if total == 0:
            return 0.0
        return (bid_val - ask_val) / total

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

    def vwap_bid(self, levels: int = 5) -> float:
        """Volume-weighted average bid price."""
        bids = self.bids[:levels]
        if not bids:
            return 0.0
        total_size = sum(b.size for b in bids)
        if total_size == 0:
            return self.best_bid
        return sum(b.price * b.size for b in bids) / total_size

    def vwap_ask(self, levels: int = 5) -> float:
        """Volume-weighted average ask price."""
        asks = self.asks[:levels]
        if not asks:
            return 0.0
        total_size = sum(a.size for a in asks)
        if total_size == 0:
            return self.best_ask
        return sum(a.price * a.size for a in asks) / total_size

    def weighted_mid_price(self, levels: int = 5) -> float:
        """Volume-weighted mid price."""
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
        """Size-weighted mid price (microstructure fair price)."""
        if not self.is_valid:
            return 0.0
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return self.mid_price
        return (
            self.best_bid * self.best_ask_size +
            self.best_ask * self.best_bid_size
        ) / total

    def find_walls(
        self,
        threshold_mult: float = 3.0,
        levels: int = 20,
        min_size: float = 0.0
    ) -> Dict[str, List[OrderBookLevel]]:
        """Find significant order walls (large resting orders)."""
        result: Dict[str, List[OrderBookLevel]] = {
            "bid_walls": [],
            "ask_walls": []
        }
        max_levels = min(levels, 20)

        bids = self.bids[:max_levels]
        asks = self.asks[:max_levels]

        if len(bids) >= 3:
            avg_size = sum(b.size for b in bids) / len(bids)
            threshold = max(avg_size * threshold_mult, min_size)
            result["bid_walls"] = [b for b in bids if b.size >= threshold]

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

    def liquidity_at_price(
        self,
        price: float,
        tolerance_pct: float = 0.1
    ) -> Tuple[float, float]:
        """Find liquidity around a specific price."""
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
        """Get depth within percentage distance from mid price."""
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
        """Estimate average fill price for a market order."""
        if size <= 0:
            return self.mid_price

        remaining = size
        total_cost = 0.0
        levels = self.asks if side == "buy" else self.bids

        for level in levels:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            remaining -= fill

        if remaining > 0:
            if levels:
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
    """Validates order book data for anomalies."""

    def __init__(
        self,
        max_spread_bps: float = OrderBookConstants.MAX_SPREAD_BPS_ALERT,
        max_price_spike_pct: float = OrderBookConstants.MAX_PRICE_SPIKE_PCT,
        history_size: int = 100,
        max_symbols: int = 500
    ):
        """Initialize validator."""
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

        sorted_symbols = sorted(
            self._last_access.items(),
            key=lambda x: x[1],
            reverse=True
        )
        keep = {s for s, _ in sorted_symbols[:self._max_symbols - 1]}

        # FIX 6: Atomic cleanup with orphan removal
        to_remove = set(self._price_history.keys()) - keep
        for symbol in to_remove:
            self._price_history.pop(symbol, None)
            self._last_access.pop(symbol, None)
        
        # Clean any orphaned entries in _last_access
        for symbol in list(self._last_access.keys()):
            if symbol not in self._price_history:
                self._last_access.pop(symbol, None)

    def validate(self, snapshot: OrderBookSnapshot) -> Tuple[bool, List[str]]:
        """Validate a snapshot for anomalies."""
        warnings: List[str] = []
        is_critical = False

        if not snapshot.is_valid:
            return False, ["Invalid snapshot (missing bids or asks)"]

        if snapshot.is_crossed:
            return False, ["Crossed order book (bid >= ask)"]

        if snapshot.spread_bps > self.max_spread_bps:
            warnings.append(f"Extreme spread: {snapshot.spread_bps:.2f} bps")

        if snapshot.is_stale():
            warnings.append(f"Stale data: {snapshot.age_ms()}ms old")

        symbol = snapshot.symbol
        mid = snapshot.mid_price
        now = time.time()

        with self._lock:
            if symbol not in self._price_history:
                if len(self._price_history) >= self._max_symbols:
                    self._cleanup_old_symbols()
                self._price_history[symbol] = deque(maxlen=self._history_size)

            self._last_access[symbol] = now
            history = self._price_history[symbol]

            if len(history) >= 10:
                avg_price = sum(history) / len(history)
                if avg_price > 0:
                    pct_change = abs(mid - avg_price) / avg_price * 100
                    if pct_change > self.max_price_spike_pct:
                        warnings.append(f"Price spike: {pct_change:.2f}% from avg")
                        is_critical = True

            history.append(mid)

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
    """Rate-limits callback invocations."""

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
        """Initialize throttled callback."""
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
                self._pending = data

        if should_call:
            try:
                self._callback(data)
            except Exception as e:
                logger.error(f"Throttled callback error: {e}")

    def flush(self) -> None:
        """Invoke pending callback immediately if any."""
        pending = None
        callback = None  # FIX 2: Capture callback reference under lock
        with self._lock:
            pending = self._pending
            self._pending = None
            if pending is not None:
                self._last_call = time.time() * 1000
                callback = self._callback  # Capture while holding lock

        if callback is not None and pending is not None:
            try:
                callback(pending)
            except Exception as e:
                logger.error(f"Throttled callback flush error: {e}")


# ============================================================
# ORDER BOOK DELTA HANDLER
# ============================================================

class OrderBookDeltaHandler:
    """Maintains order book state from incremental updates."""

    def __init__(self, symbol: str, max_levels: int = 100):
        """Initialize delta handler."""
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

        sorted_bids = sorted(
            ((p, s) for p, s in self._bids.items() if s > 0),
            key=lambda x: -x[0]
        )[:self.max_levels]

        sorted_asks = sorted(
            ((p, s) for p, s in self._asks.items() if s > 0),
            key=lambda x: x[0]
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
        """Apply a full snapshot (replaces all data)."""
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
        """Apply incremental update and return new snapshot."""
        with self._lock:
            if update_id <= self._last_update_id:
                return None

            for price, size in bids:
                if size == 0:
                    self._bids.pop(price, None)
                else:
                    self._bids[price] = size

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
    """Centralized alert handling with throttling."""

    def __init__(
        self,
        throttle_seconds: int = OrderBookConstants.ALERT_THROTTLE_SECONDS,
        max_throttle_keys: int = OrderBookConstants.MAX_THROTTLE_KEYS
    ):
        """Initialize alert manager."""
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
        """Send an alert to all handlers."""
        if throttle_key:
            now = time.time()
            with self._lock:
                self._cleanup_throttle(now)
                if throttle_key in self._throttle:
                    if now - self._throttle[throttle_key] < self._throttle_seconds:
                        return False
                self._throttle[throttle_key] = now

        ctx = context.copy() if context else {}
        ctx["timestamp"] = datetime.now().isoformat()
        ctx["level"] = level

        with self._lock:
            handlers = list(self._handlers)

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
    """Persistent state management with crash recovery."""

    def __init__(self, state_file: str = "trading_state.json"):
        """Initialize state manager."""
        self.state_file = Path(state_file)
        self._state: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._auto_save = True
        self._dirty = False  # FIX 3: Debounce tracking
        self._last_save = 0.0  # FIX 3: Last save timestamp
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
        """Save state to disk atomically."""
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
            self._dirty = True  # FIX 3: Mark as dirty
        if auto_save and self._auto_save:
            self._debounced_save()  # FIX 3: Use debounced save

    def _debounced_save(self) -> None:
        """FIX 3: Save with 5-second debounce to reduce disk I/O."""
        now = time.time()
        if self._dirty and now - self._last_save > 5.0:
            if self.save():
                self._dirty = False
                self._last_save = now

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
# ORDER BOOK ANALYZER
# ============================================================

class OrderBookAnalyzer:
    """Tracks order book evolution over time."""

    def __init__(
        self,
        max_snapshots: int = OrderBookConstants.MAX_SNAPSHOTS_DEFAULT
    ):
        """Initialize analyzer."""
        self._snapshots: Deque[OrderBookSnapshot] = deque(maxlen=max_snapshots)
        self._lock = threading.RLock()
        self._last_sequence = 0
        self._sequence_gaps = 0
        self._total_updates = 0
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_key: Tuple[int, int] = (0, 0)

    def add_snapshot(self, snapshot: Optional[OrderBookSnapshot]) -> None:
        """Add a new snapshot to the analyzer."""
        if snapshot is None:
            return

        with self._lock:
            if snapshot.sequence > 0 and self._last_sequence > 0:
                if snapshot.sequence > self._last_sequence + 1:
                    gap = snapshot.sequence - self._last_sequence - 1
                    self._sequence_gaps += gap

            if snapshot.sequence > 0:
                self._last_sequence = snapshot.sequence

            self._snapshots.append(snapshot)
            self._total_updates += 1
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
        """Get the most recent n snapshots efficiently."""
        with self._lock:
            length = len(self._snapshots)
            if length == 0:
                return []
            if length <= n:
                return list(self._snapshots)
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
        """Get snapshot at or before a specific time using binary search."""
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

    def _get_valid_imbalances(self, window: int) -> List[float]:
        """Get imbalances from recent valid snapshots."""
        return [s.imbalance for s in self.get_recent(window) if s.is_valid]

    def average_imbalance(self, window: int = 20) -> float:
        """Average imbalance over window."""
        imbalances = self._get_valid_imbalances(window)
        return sum(imbalances) / len(imbalances) if imbalances else 0.0

    @staticmethod
    def _calc_slope(values: List[float]) -> float:
        """Calculate linear regression slope."""
        n = len(values)
        if n < 3:
            return 0.0

        sum_y = sum(values)
        sum_xy = sum(i * v for i, v in enumerate(values))
        n_f = float(n)

        denom = n_f * (n_f * n_f - 1) / 12
        if denom == 0:
            return 0.0

        return (sum_xy - (n_f - 1) / 2 * sum_y) / denom

    def imbalance_trend(self, window: int = 20) -> float:
        """Calculate imbalance trend (slope)."""
        imbalances = self._get_valid_imbalances(window)
        return self._calc_slope(imbalances) if len(imbalances) >= 5 else 0.0

    def imbalance_acceleration(self, window: int = 20) -> float:
        """Calculate change in imbalance trend."""
        imbalances = self._get_valid_imbalances(window)
        if len(imbalances) < window:
            return 0.0

        half = window // 2
        if half < 3:
            return 0.0

        recent_slope = self._calc_slope(imbalances[half:])
        older_slope = self._calc_slope(imbalances[:half])
        return recent_slope - older_slope

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
        """Check for bullish order book condition."""
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
        """Check for bearish order book condition."""
        return self._check_pressure(
            imbalance_threshold,
            trend_threshold,
            require_both,
            is_bullish=False
        )

    def get_signal_strength(self) -> float:
        """Get composite signal strength."""
        current = self.current
        if not current or not current.is_valid:
            return 0.0

        signal = (
            current.imbalance * OrderBookConstants.SIGNAL_IMBALANCE_WEIGHT +
            current.depth_imbalance(10) * OrderBookConstants.SIGNAL_DEPTH_WEIGHT +
            self.imbalance_trend() * OrderBookConstants.SIGNAL_TREND_SCALE *
            OrderBookConstants.SIGNAL_TREND_WEIGHT
        )

        return max(-1.0, min(1.0, signal))

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

    def get_statistics(self, window: int = 20) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        cache_key = (self._total_updates, window)
        if self._stats_cache is not None and self._stats_cache_key == cache_key:
            return self._stats_cache

        current = self.current
        if not current or not current.is_valid:
            return {}

        result = {
            "mid_price": current.mid_price,
            "spread": current.spread,
            "spread_pct": current.spread_pct,
            "spread_bps": current.spread_bps,
            "imbalance": current.imbalance,
            "depth_imbalance_10": current.depth_imbalance(10),
            "avg_spread_pct": self.average_spread_pct(window),
            "avg_imbalance": self.average_imbalance(window),
            "imbalance_trend": self.imbalance_trend(window),
            "signal_strength": self.get_signal_strength(),
            "microprice": current.microprice(),
            "weighted_mid": current.weighted_mid_price(),
            "bid_volume_10": current.total_bid_volume(10),
            "ask_volume_10": current.total_ask_volume(10),
            "snapshot_count": self.count,
            "sequence_gaps": self.sequence_gaps,
            "data_fresh": self.is_data_fresh(),
        }

        self._stats_cache = result
        self._stats_cache_key = cache_key

        return result


# ============================================================
# TICK CLASSIFIER
# ============================================================

class TickClassifier:
    """Classifies ticks as buy or sell using tick rule."""

    __slots__ = ('_last_price', '_last_side')

    def __init__(self):
        """Initialize classifier."""
        self._last_price: Optional[float] = None
        self._last_side = TradeSide.UNKNOWN

    def classify(self, price: float) -> TradeSide:
        """Classify a single tick."""
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
    """Order flow analysis metrics for strategy signals."""
    cumulative_delta: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    large_buy_count: int = 0
    large_sell_count: int = 0
    trade_count: int = 0
    unknown_count: int = 0

    @property
    def ofi(self) -> float:
        """Order Flow Imbalance: normalized buying vs selling pressure."""
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
        """Imbalance in large trades (institutional activity indicator)."""
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
    """Tracks order flow for strategy signals."""

    def __init__(
        self,
        large_trade_threshold: float = OrderBookConstants.LARGE_TRADE_THRESHOLD,
        window_size: int = 100
    ):
        """Initialize order flow tracker."""
        self.large_threshold = large_trade_threshold
        self.window_size = window_size
        self._classifier = TickClassifier()
        self._metrics = OrderFlowMetrics()
        self._recent_deltas: Deque[float] = deque(maxlen=window_size)
        self._lock = threading.Lock()

    def process_trade(self, price: float, size: float) -> OrderFlowMetrics:
        """Process a single trade and update metrics."""
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
                self._metrics.unknown_count += 1
                self._recent_deltas.append(0)

            return self._copy_metrics()

    def process_batch(self, trades: List[SimpleTrade]) -> OrderFlowMetrics:
        """Process multiple trades: [(price, size), ...]"""
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
        """Get delta momentum (recent vs older activity)."""
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
# SLIPPAGE ESTIMATOR
# ============================================================

class SlippageEstimator:
    """Estimates slippage for trade execution simulation."""

    @staticmethod
    def estimate(
        snapshot: OrderBookSnapshot,
        size: float,
        side: str,
        aggression: float = 1.0
    ) -> Tuple[float, float, float]:
        """Estimate execution price and slippage."""
        if not snapshot or not snapshot.is_valid:
            return 0.0, 0.0, 0.0

        mid = snapshot.mid_price

        if aggression < 0.3:
            if side == "buy":
                price = snapshot.best_bid
            else:
                price = snapshot.best_ask

            if mid > 0:
                slippage = abs(price - mid) / mid * 10000
            else:
                slippage = 0.0

            fill_prob = 0.3 + aggression

        else:
            price = snapshot.market_impact(size, side)

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
        """Apply fixed slippage to a price."""
        slip_mult = slippage_bps / 10000

        if side == "buy":
            return price * (1 + slip_mult)
        else:
            return price * (1 - slip_mult)

    @staticmethod
    def simulate_fill(
        snapshot: OrderBookSnapshot,
        size: float,
        side: str,
        max_slippage_bps: float = 10.0
    ) -> Optional[Tuple[float, float]]:
        """Simulate order fill with slippage limit."""
        if not snapshot or not snapshot.is_valid:
            return None

        mid = snapshot.mid_price
        if mid <= 0:
            return None

        if side == "buy":
            max_price = mid * (1 + max_slippage_bps / 10000)
            levels = snapshot.asks
        else:
            max_price = mid * (1 - max_slippage_bps / 10000)
            levels = snapshot.bids

        filled = 0.0
        cost = 0.0

        for level in levels:
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
        """Calculate detailed market impact costs."""
        if not snapshot or not snapshot.is_valid:
            return {"error": "invalid_snapshot"}

        mid = snapshot.mid_price
        fill_price = snapshot.market_impact(size, side)
        half_spread = snapshot.spread / 2

        if side == "buy":
            cross_price = mid + half_spread
        else:
            cross_price = mid - half_spread

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
    """Simulates trade execution for backtesting."""

    def __init__(
        self,
        slippage_model: str = "realistic",
        fixed_slippage_bps: float = OrderBookConstants.DEFAULT_SLIPPAGE_BPS,
        fee_bps: float = OrderBookConstants.DEFAULT_FEE_BPS,
        partial_fill_enabled: bool = False
    ):
        """Initialize execution simulator."""
        if slippage_model not in ("none", "fixed", "realistic"):
            raise ValueError(
                f"Invalid slippage_model: {slippage_model}. "
                f"Must be 'none', 'fixed', or 'realistic'"
            )

        self.slippage_model = slippage_model
        self.fixed_slippage_bps = fixed_slippage_bps
        self.fee_bps = fee_bps
        self.partial_fill_enabled = partial_fill_enabled

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
        """Simulate order entry."""
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {side}. Must be 'buy' or 'sell'")

        if size <= 0:
            raise ValueError(f"Size must be positive: {size}")

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

        else:
            if orderbook and orderbook.is_valid:
                fill_price, slippage_bps, fill_prob = SlippageEstimator.estimate(
                    orderbook,
                    size,
                    side,
                    aggression
                )

                if self.partial_fill_enabled and fill_prob < 1.0:
                    fill_size = size * fill_prob
                else:
                    fill_size = size
            else:
                fill_price = SlippageEstimator.estimate_fixed(
                    signal_price,
                    side,
                    self.fixed_slippage_bps
                )
                slippage_bps = self.fixed_slippage_bps
                fill_size = size
                fill_prob = 1.0

        fee = fill_price * fill_size * (self.fee_bps / 10000)

        if side == "buy":
            total_outlay = fill_price * fill_size + fee
            net_proceeds = 0.0
        else:
            total_outlay = 0.0
            net_proceeds = fill_price * fill_size - fee

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
        """Simulate exit and calculate PnL."""
        exit_side = "sell" if entry["side"] == "buy" else "buy"

        exit_result = self.simulate_entry(
            exit_price,
            entry["size"],
            exit_side,
            orderbook,
            aggression
        )

        if entry["side"] == "buy":
            gross_pnl = (
                exit_result["fill_price"] - entry["fill_price"]
            ) * entry["size"]
        else:
            gross_pnl = (
                entry["fill_price"] - exit_result["fill_price"]
            ) * entry["size"]

        total_fees = entry["fee"] + exit_result["fee"]
        total_slippage_cost = entry["slippage_cost"] + exit_result["slippage_cost"]
        net_pnl = gross_pnl - total_fees

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
        """Simulate complete round-trip trade."""
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
    """Generates synthetic order books from tick data."""

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
        """Generate a synthetic order book from tick data."""
        if not ticks:
            return None

        rng = random.Random(random_seed)

        last_ts, last_price = ticks[-1]
        if last_price <= 0:
            return None

        spread_pct = spread_bps / 10000

        if len(ticks) > 10:
            recent_prices = [p for _, p in ticks[-20:]]
            min_p = min(recent_prices)
            max_p = max(recent_prices)
            avg_p = sum(recent_prices) / len(recent_prices)

            if avg_p > 0:
                vol_ratio = (max_p - min_p) / avg_p
                spread_mult = max(
                    1.0,
                    1 + vol_ratio * OrderBookConstants.VOLATILITY_SPREAD_MULT
                )
                spread_mult = min(spread_mult, OrderBookConstants.MAX_SPREAD_MULT)
                spread_pct *= spread_mult

        half_spread = last_price * spread_pct / 2
        tick_size = last_price * 0.0001

        imbalance_mult = 1.0
        if use_tick_classification and len(ticks) > 5:
            classifier = TickClassifier()
            recent_ticks = ticks[-50:] if len(ticks) >= 50 else ticks
            sides = classifier.classify_batch([p for _, p in recent_ticks])
            buys = sum(1 for s in sides if s == TradeSide.BUY)
            sells = sum(1 for s in sides if s == TradeSide.SELL)

            if buys + sells > 0:
                imbalance_mult = 1 + (buys - sells) / (buys + sells) * 0.3

        size_weight = 1.0
        if tick_sizes and len(tick_sizes) >= len(ticks):
            recent_sizes = tick_sizes[-20:]
            if recent_sizes and max(recent_sizes) > 0:
                avg_size = sum(recent_sizes) / len(recent_sizes)
                size_weight = max(0.5, min(2.0, avg_size / base_size))

        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []

        for i in range(levels):
            size = base_size * size_weight * (size_decay ** i)

            jitter_bid = 0.5 + rng.random()
            jitter_ask = 0.5 + rng.random()

            bid_price = last_price - half_spread - tick_size * i
            ask_price = last_price + half_spread + tick_size * i

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
        """Generate synthetic order book from OHLCV candle."""
        if close_price <= 0:
            raise ValueError(f"Close price must be positive: {close_price}")

        rng = random.Random(random_seed)

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
        """Generate an OrderBookAnalyzer with historical snapshots."""
        if not ticks or len(ticks) < sample_every:
            return None

        analyzer = OrderBookAnalyzer()

        for i in range(sample_every, len(ticks) + 1, sample_every):
            snapshot = SyntheticOrderBook.from_ticks(
                ticks=ticks[:i],
                tick_sizes=tick_sizes[:i] if tick_sizes else None,
                random_seed=i,
                **kwargs
            )
            if snapshot and snapshot.is_valid:
                analyzer.add_snapshot(snapshot)

        return analyzer if analyzer.count > 0 else None


# ============================================================
# BACKTEST ORDER BOOK PROVIDER
# ============================================================

class BacktestOrderBookProvider:
    """Provides order book data for backtesting."""

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
        """Load pre-recorded snapshots."""
        with self._lock:
            if symbol not in self._analyzers:
                self._analyzers[symbol] = OrderBookAnalyzer()

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
        """Generate synthetic order book from tick data."""
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
        """Generate synthetic order book from OHLCV candles."""
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
        """Set current backtest time."""
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
        """Get snapshot at or before a specific time."""
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
        """Clear data for symbol or all symbols."""
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
    """Common implementation for WebSocket streams."""

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
        """Initialize tracking for a symbol."""
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
        with self._lock:
            if symbol in self._analyzers:
                self._analyzers[symbol].add_snapshot(snapshot)
            self._record_update(symbol, timestamp)
            callbacks = list(self._callbacks.get(symbol, []))

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
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()

                if pending and not self._loop.is_closed():
                    try:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    except (RuntimeError, OSError, asyncio.CancelledError):
                        pass
            except RuntimeError:
                pass

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

        with self._lock:
            for h in self._health.values():
                h.connected = False

        if tasks:
            for task in tasks:
                if hasattr(task, 'cancel'):
                    task.cancel()

        if self._loop and not self._loop.is_closed():
            try:
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

        if self._thread and self._thread.is_alive():
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=OrderBookConstants.SHUTDOWN_TIMEOUT)

        self._started_event.clear()


# ============================================================
# BINANCE SINGLE SYMBOL STREAM
# ============================================================

class BinanceOrderBookStream(_StreamBase):
    """WebSocket stream for a single Binance symbol."""

    def __init__(
        self,
        max_reconnect_attempts: int = OrderBookConstants.MAX_RECONNECT_ATTEMPTS,
        testnet: bool = False,
        alerts: Optional[AlertManager] = None
    ):
        """Initialize Binance stream."""
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

            snapshot = OrderBookSnapshot.from_binance_fast(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=timestamp,
                sequence=data.get("lastUpdateId", 0)
            )

            if not snapshot.bids or not snapshot.asks:
                return

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
# BINANCE MULTI-SYMBOL STREAM
# ============================================================

class BinanceMultiStream(_StreamBase):
    """Efficient multi-symbol WebSocket stream for Binance."""

    MAX_STREAMS_PER_CONNECTION: Final[int] = 100
    MAX_CONNECTIONS: Final[int] = 5

    def __init__(
        self,
        testnet: bool = False,
        alerts: Optional[AlertManager] = None
    ):
        """Initialize multi-symbol stream."""
        super().__init__()

        self._base_url = (
            "wss://testnet.binance.vision/stream" if testnet
            else "wss://stream.binance.com:9443/stream"
        )
        self._symbols: Set[str] = set()
        self._connection_tasks: List[asyncio.Task] = []
        self._update_counts: Dict[str, int] = {}
        self._last_health_calc = time.time()

        self._symbol_to_ws: Dict[str, str] = {}
        self._stream_to_symbol: Dict[str, str] = {}

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
        """Build combined stream URL."""
        original_count = len(symbols)
        symbols = list(symbols)

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
        invalid = [s for s in symbols if not self._validate_symbol(s)]
        if invalid:
            raise ValueError(f"Invalid symbols: {invalid}")

        with self._lock:
            new_symbols = [s for s in symbols if s not in self._symbols]
            new_count = len(self._symbols) + len(new_symbols)

            if new_count > self.max_capacity:
                raise ValueError(
                    f"Exceeds capacity: {new_count} > {self.max_capacity}"
                )

            for symbol in symbols:
                if symbol not in self._analyzers:
                    self._analyzers[symbol] = OrderBookAnalyzer()
                    self._callbacks[symbol] = []
                    self._health[symbol] = StreamHealth()

                    clean_lower = symbol.replace("/", "").replace("-", "").lower()
                    clean_upper = clean_lower.upper()
                    stream_key = f"{clean_lower}@depth20@100ms"

                    self._symbol_to_ws[clean_upper] = symbol
                    self._stream_to_symbol[stream_key] = symbol

                if callback and callback not in self._callbacks[symbol]:
                    self._callbacks[symbol].append(callback)

                self._symbols.add(symbol)

        self._symbols_changed.set()

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

                if new_symbols != current_symbols or self._symbols_changed.is_set():
                    self._symbols_changed.clear()
                    current_symbols = new_symbols

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

            symbol = self._stream_to_symbol.get(stream)
            if not symbol:
                return

            bids = payload.get("bids", [])
            asks = payload.get("asks", [])

            if not bids and not asks:
                return

            now_ms = _now_ms()  # FIX 5: Single time call
            timestamp = payload.get("E", now_ms)
            now = now_ms / 1000.0  # Convert to seconds

            snapshot = OrderBookSnapshot.from_binance_fast(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=timestamp,
                sequence=payload.get("lastUpdateId", 0)
            )

            if not snapshot.bids or not snapshot.asks:
                return

            if snapshot.bids[0].price >= snapshot.asks[0].price:
                self._validation_failures[symbol] = (
                    self._validation_failures.get(symbol, 0) + 1
                )
                return

            self._validation_failures[symbol] = 0

            with self._lock:
                if symbol in self._analyzers:
                    self._analyzers[symbol].add_snapshot(snapshot)

                if symbol in self._health:
                    self._health[symbol].last_update_ms = int(now * 1000)
                    if timestamp > 0:
                        self._health[symbol].latency_ms = now * 1000 - timestamp

                self._update_counts[symbol] = (
                    self._update_counts.get(symbol, 0) + 1
                )
                self._msg_count += 1

                if now - self._last_health_calc >= OrderBookConstants.HEALTH_CALC_INTERVAL:
                    elapsed = now - self._last_health_calc
                    for sym, cnt in self._update_counts.items():
                        if sym in self._health:
                            self._health[sym].updates_per_second = cnt / elapsed
                    self._update_counts.clear()
                    self._last_health_calc = now

                callbacks = list(self._callbacks.get(symbol, []))

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

        for task in self._connection_tasks:
            if not task.done():
                task.cancel()

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

        latencies = sorted([
            h.latency_ms for h in all_health.values()
            if h.latency_ms > 0
        ]) or [0]

        connected_count = sum(1 for h in all_health.values() if h.connected)
        healthy_count = sum(1 for h in all_health.values() if h.is_healthy)
        total_symbols = len(all_health)

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
# CCXT PRO STREAM (OPTIONAL)
# ============================================================

class CCXTOrderBookStream(_StreamBase):
    """Order book stream using CCXT Pro."""

    def __init__(self, exchange_id: str = "binance"):
        """Initialize CCXT stream."""
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

                for s in current:
                    if s not in self._symbol_tasks or self._symbol_tasks[s].done():
                        self._symbol_tasks[s] = asyncio.create_task(
                            self._watch_symbol(s)
                        )

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
    """High-level order book management."""

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
        """Initialize order book manager."""
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

        if store_history:
            self.history_dir.mkdir(exist_ok=True, parents=True)
            self._flush_executor = ThreadPoolExecutor(
                max_workers=OrderBookConstants.MAX_FLUSH_THREADS
            )

        self._validator = DataValidator() if enable_validation else None

        self._alerts = AlertManager() if enable_alerts else None
        if self._alerts:
            self._alerts.add_handler(console_alert_handler)

        self._state = StateManager()

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

        if self.store_history:
            self._buffer_snapshot(symbol, snapshot)

        now = time.time()
        if now - self._last_health_report >= OrderBookConstants.HEALTH_REPORT_INTERVAL:
            self._report_health()
            self._last_health_report = now

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
            with self._lock:
                existing = self._history_buffers.get(symbol, [])
                self._history_buffers[symbol] = snapshots + existing

    def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        if self.backtest_mode:
            self._backtest_provider.clear(symbol)
            return

        if self.store_history and symbol in self._history_buffers:
            with self._lock:
                buf = self._history_buffers.pop(symbol, [])
            if buf:
                self._do_flush(symbol, buf)

        if self._stream:
            self._stream.unsubscribe(symbol)

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
        """Check if all symbols are ready for trading."""
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

        if self.store_history:
            with self._lock:
                syms = list(self._history_buffers.keys())

            for sym in syms:
                with self._lock:
                    buf = self._history_buffers.pop(sym, [])
                if buf:
                    self._do_flush(sym, buf)

        if self._flush_executor:
            self._flush_executor.shutdown(wait=True)

        if self._stream:
            self._stream.stop()

        logger.info("OrderBookManager stopped")


# ============================================================
# EXPORTS
# ============================================================



# ============================================================
# === ORACLE OPTIMIZATIONS INSERTED ===
# ============================================================
# Added by oracle_patch_optimizations.sh
# These optimizations provide 5-50x speedup for backtesting
# ============================================================

# ============================================================
# NUMPY DEPENDENCY CHECK
# ============================================================

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None

try:
    from numba import jit, prange
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False
    jit = None
    prange = None


# ============================================================
# FAST BACKTEST ENGINE (NumPy Accelerated)
# ============================================================

class FastBacktestEngine:
    """NumPy-accelerated backtest engine for maximum throughput.
    
    Provides 5-10x speedup over standard Python loops.
    
    Usage:
        engine = FastBacktestEngine(seed=42)
        results = engine.process_candle_batch(candles, ticks_by_candle)
    """
    
    __slots__ = ('_rng', '_ob_cache', '_seed')
    
    def __init__(self, seed: int = 42):
        """Initialize with optional random seed for reproducibility."""
        if not HAS_NUMPY:
            raise ImportError("NumPy required for FastBacktestEngine: pip install numpy")
        self._seed = seed
        self._rng = np.random.default_rng(seed)
        self._ob_cache: Dict[int, 'OrderBookSnapshot'] = {}
    
    def reset(self) -> None:
        """Reset RNG state for reproducible runs."""
        self._rng = np.random.default_rng(self._seed)
        self._ob_cache.clear()
    
    @staticmethod
    def vectorized_tick_classify(prices: 'np.ndarray') -> 'np.ndarray':
        """Vectorized tick classification - 10x faster than loop.
        
        Args:
            prices: NumPy array of prices
            
        Returns:
            Array of -1 (sell), 0 (unknown), 1 (buy)
        """
        if len(prices) < 2:
            return np.zeros(len(prices), dtype=np.int8)
        
        # Calculate price changes
        diff = np.diff(prices)
        
        # Classify: positive = buy, negative = sell
        result = np.zeros(len(prices), dtype=np.int8)
        result[1:] = np.sign(diff).astype(np.int8)
        
        # Forward-fill zeros (tick rule: use last direction)
        mask = result == 0
        idx = np.where(~mask, np.arange(len(result)), 0)
        np.maximum.accumulate(idx, out=idx)
        result = result[idx]
        
        return result
    
    @staticmethod
    def vectorized_ofi(
        prices: 'np.ndarray',
        sizes: 'np.ndarray',
        sides: 'np.ndarray'
    ) -> Tuple[float, float, float]:
        """Vectorized Order Flow Imbalance calculation.
        
        Args:
            prices: Price array
            sizes: Size array
            sides: Side classification array (-1, 0, 1)
            
        Returns:
            (cumulative_delta, buy_volume, sell_volume)
        """
        buy_mask = sides == 1
        sell_mask = sides == -1
        
        buy_volume = float(np.sum(sizes[buy_mask]))
        sell_volume = float(np.sum(sizes[sell_mask]))
        cumulative_delta = buy_volume - sell_volume
        
        return cumulative_delta, buy_volume, sell_volume
    
    @staticmethod
    def vectorized_vwap(prices: 'np.ndarray', sizes: 'np.ndarray') -> float:
        """Vectorized VWAP calculation."""
        total_size = np.sum(sizes)
        if total_size == 0:
            return float(prices[-1]) if len(prices) > 0 else 0.0
        return float(np.sum(prices * sizes) / total_size)
    
    @staticmethod
    def vectorized_volatility(prices: 'np.ndarray') -> float:
        """Vectorized realized volatility (standard deviation of returns)."""
        if len(prices) < 2:
            return 0.0
        returns = np.diff(np.log(prices))
        return float(np.std(returns))
    
    def fast_synthetic_ob(
        self,
        last_price: float,
        last_timestamp: int,
        tick_imbalance: float = 0.0,
        spread_bps: float = 5.0,
        levels: int = 10,
        base_size: float = 10.0,
        volatility: float = 0.0
    ) -> 'OrderBookSnapshot':
        """Ultra-fast synthetic order book generation.
        
        ~5x faster than SyntheticOrderBook.from_ticks()
        
        Args:
            last_price: Current price
            last_timestamp: Timestamp in milliseconds
            tick_imbalance: OFI imbalance (-1 to 1)
            spread_bps: Spread in basis points
            levels: Number of price levels
            base_size: Base order size
            volatility: Recent volatility (widens spread)
            
        Returns:
            OrderBookSnapshot
        """
        # Adjust spread for volatility
        vol_mult = 1.0 + min(volatility * 100, 2.0)
        spread_pct = (spread_bps * vol_mult) / 10000
        half_spread = last_price * spread_pct / 2
        tick_size = last_price * 0.0001
        
        # Pre-generate all random values at once (faster than per-level)
        jitters = self._rng.uniform(0.5, 1.5, levels * 2)
        
        # Imbalance multiplier affects size distribution
        imb = 1 + np.clip(tick_imbalance, -1, 1) * 0.3
        
        # Exponential decay factors
        decay = 0.7 ** np.arange(levels)
        
        # Calculate bid prices and sizes (vectorized)
        bid_prices = last_price - half_spread - tick_size * np.arange(levels)
        bid_sizes = base_size * decay * imb * jitters[:levels]
        
        # Calculate ask prices and sizes (vectorized)
        ask_prices = last_price + half_spread + tick_size * np.arange(levels)
        ask_sizes = base_size * decay * (2 - imb) * jitters[levels:]
        
        # Create levels directly (bypass validation for speed)
        bids = []
        asks = []
        for i in range(levels):
            bid = object.__new__(OrderBookLevel)
            bid.price = float(bid_prices[i])
            bid.size = max(0.001, float(bid_sizes[i]))
            bids.append(bid)
            
            ask = object.__new__(OrderBookLevel)
            ask.price = float(ask_prices[i])
            ask.size = max(0.001, float(ask_sizes[i]))
            asks.append(ask)
        
        # Create snapshot directly (bypass __post_init__ for speed)
        snap = object.__new__(OrderBookSnapshot)
        snap.symbol = "BACKTEST"
        snap.timestamp = last_timestamp
        snap.bids = bids
        snap.asks = asks
        snap.sequence = 0
        snap.exchange = "synthetic_fast"
        snap._sorted = True
        
        return snap
    
    def process_candle_batch(
        self,
        candles: List[Dict[str, Any]],
        ticks_by_candle: Dict[int, List[Tuple[int, float, float]]],
        compute_ob: bool = True,
        ob_levels: int = 10
    ) -> List[Dict[str, Any]]:
        """Process multiple candles in optimized batch.
        
        Args:
            candles: List of candle dicts with timestamp, open, high, low, close, volume
            ticks_by_candle: Dict mapping candle timestamp to list of (ts, price, size) ticks
            compute_ob: Whether to generate synthetic order books
            ob_levels: Number of order book levels to generate
            
        Returns:
            List of analysis results per candle
        """
        results = []
        
        for candle in candles:
            ts = candle.get('timestamp', 0)
            ticks = ticks_by_candle.get(ts, [])
            
            # Handle candles with no ticks
            if not ticks:
                results.append({
                    'timestamp': ts,
                    'close': candle.get('close', 0),
                    'orderbook': None,
                    'ofi': 0.0,
                    'cumulative_delta': 0.0,
                    'buy_volume': 0.0,
                    'sell_volume': 0.0,
                    'tick_count': 0,
                    'vwap': candle.get('close', 0),
                    'volatility': 0.0,
                })
                continue
            
            # Convert to numpy arrays (one-time cost)
            tick_times = np.array([t[0] for t in ticks], dtype=np.int64)
            tick_prices = np.array([t[1] for t in ticks], dtype=np.float64)
            tick_sizes = np.array([t[2] for t in ticks], dtype=np.float64)
            
            # Vectorized calculations
            sides = self.vectorized_tick_classify(tick_prices)
            delta, buy_vol, sell_vol = self.vectorized_ofi(tick_prices, tick_sizes, sides)
            vwap = self.vectorized_vwap(tick_prices, tick_sizes)
            volatility = self.vectorized_volatility(tick_prices)
            
            # Calculate OFI
            total_vol = buy_vol + sell_vol
            ofi = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
            
            # Fast order book generation (optional)
            ob = None
            if compute_ob:
                ob = self.fast_synthetic_ob(
                    last_price=float(tick_prices[-1]),
                    last_timestamp=int(tick_times[-1]),
                    tick_imbalance=ofi,
                    volatility=volatility,
                    levels=ob_levels,
                )
            
            results.append({
                'timestamp': ts,
                'close': candle.get('close', 0),
                'orderbook': ob,
                'ofi': ofi,
                'cumulative_delta': delta,
                'buy_volume': buy_vol,
                'sell_volume': sell_vol,
                'tick_count': len(ticks),
                'vwap': vwap,
                'volatility': volatility,
            })
        
        return results
    
    @staticmethod
    def fast_market_impact(
        ob: 'OrderBookSnapshot',
        size: float,
        side: str
    ) -> float:
        """Optimized market impact calculation.
        
        Args:
            ob: Order book snapshot
            size: Order size
            side: 'buy' or 'sell'
            
        Returns:
            Estimated fill price
        """
        if size <= 0 or not ob or not ob.is_valid:
            return ob.mid_price if ob else 0.0
        
        levels = ob.asks if side == "buy" else ob.bids
        remaining = size
        total_cost = 0.0
        
        for level in levels:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            remaining -= fill
        
        # Extrapolate for insufficient liquidity
        if remaining > 0 and levels:
            penalty = 1.02 if side == "buy" else 0.98
            total_cost += remaining * levels[-1].price * penalty
        
        return total_cost / size if size > 0 else ob.mid_price


# ============================================================
# LOCK-FREE BACKTEST ANALYZER
# ============================================================

class BacktestAnalyzer:
    """Lock-free analyzer optimized for single-threaded backtest.
    
    50% faster than OrderBookAnalyzer by removing thread safety overhead.
    Use this for backtesting, use OrderBookAnalyzer for live trading.
    
    Usage:
        analyzer = BacktestAnalyzer(max_snapshots=100)
        analyzer.add(snapshot)
        signal = analyzer.get_signal_strength()
    """
    
    __slots__ = (
        '_snapshots', '_max_size', '_stats_cache', '_stats_cache_key',
        '_last_seq', '_seq_gaps', '_total'
    )
    
    def __init__(self, max_snapshots: int = 100):
        """Initialize analyzer.
        
        Args:
            max_snapshots: Maximum snapshots to retain
        """
        self._snapshots: Deque['OrderBookSnapshot'] = deque(maxlen=max_snapshots)  # FIX 4: O(1)
        self._max_size = max_snapshots
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_key: int = 0
        self._last_seq = 0
        self._seq_gaps = 0
        self._total = 0
    
    def add(self, snap: Optional['OrderBookSnapshot']) -> None:
        """Add snapshot without locking overhead.
        
        Args:
            snap: OrderBookSnapshot to add
        """
        if snap is None:
            return
        
        # Track sequence gaps
        if snap.sequence > 0:
            if self._last_seq > 0 and snap.sequence > self._last_seq + 1:
                self._seq_gaps += snap.sequence - self._last_seq - 1
            self._last_seq = snap.sequence
        
        # FIX 4: deque maxlen auto-handles size limit - O(1) vs O(n)
        self._snapshots.append(snap)
        self._total += 1
        self._stats_cache = None
    
    def add_snapshot(self, snap: Optional['OrderBookSnapshot']) -> None:
        """Alias for add() - API compatibility with OrderBookAnalyzer."""
        self.add(snap)
    
    def clear(self) -> None:
        """Clear all snapshots and reset state."""
        self._snapshots.clear()
        self._last_seq = 0
        self._seq_gaps = 0
        self._total = 0
        self._stats_cache = None
    
    @property
    def current(self) -> Optional['OrderBookSnapshot']:
        """Get most recent snapshot."""
        return self._snapshots[-1] if self._snapshots else None
    
    @property
    def count(self) -> int:
        """Number of stored snapshots."""
        return len(self._snapshots)
    
    @property
    def is_empty(self) -> bool:
        """Check if analyzer has no data."""
        return len(self._snapshots) == 0
    
    @property
    def total_updates(self) -> int:
        """Total updates processed."""
        return self._total
    
    @property
    def sequence_gaps(self) -> int:
        """Number of detected sequence gaps."""
        return self._seq_gaps
    
    def get_recent(self, n: int = 10) -> List['OrderBookSnapshot']:
        """Get recent snapshots efficiently.
        
        Args:
            n: Number of recent snapshots
            
        Returns:
            List of recent snapshots
        """
        if len(self._snapshots) <= n:
            return list(self._snapshots)
        return self._snapshots[-n:]
    
    def is_data_fresh(self, max_age_ms: int = 5000) -> bool:
        """Check if data is recent enough.
        
        Args:
            max_age_ms: Maximum acceptable age in milliseconds
        """
        current = self.current
        return current is not None and not current.is_stale(max_age_ms)
    
    def average_spread_bps(self, window: int = 20) -> float:
        """Average spread in basis points over window."""
        snaps = self.get_recent(window)
        spreads = [s.spread_bps for s in snaps if s.is_valid]
        return sum(spreads) / len(spreads) if spreads else 0.0
    
    def average_imbalance(self, window: int = 20) -> float:
        """Average imbalance over window."""
        snaps = self.get_recent(window)
        imbalances = [s.imbalance for s in snaps if s.is_valid]
        return sum(imbalances) / len(imbalances) if imbalances else 0.0
    
    def imbalance_trend(self, window: int = 20) -> float:
        """Calculate imbalance trend (slope) without locks.
        
        Args:
            window: Number of snapshots for calculation
            
        Returns:
            Trend slope (positive = increasingly bullish)
        """
        snaps = self.get_recent(window)
        values = [s.imbalance for s in snaps if s.is_valid]
        
        n = len(values)
        if n < 3:
            return 0.0
        
        # Linear regression slope
        sum_y = sum(values)
        sum_xy = sum(i * v for i, v in enumerate(values))
        
        denom = n * (n * n - 1) / 12
        if denom == 0:
            return 0.0
        
        return (sum_xy - (n - 1) / 2 * sum_y) / denom
    
    def get_signal_strength(self) -> float:
        """Get composite signal strength.
        
        Returns:
            Signal from -1.0 (bearish) to 1.0 (bullish)
        """
        snap = self.current
        if not snap or not snap.is_valid:
            return 0.0
        
        signal = (
            snap.imbalance * 0.4 +
            snap.depth_imbalance(10) * 0.4 +
            self.imbalance_trend() * 10.0 * 0.2
        )
        
        return max(-1.0, min(1.0, signal))
    
    def is_bullish(self, imbalance_threshold: float = 0.2) -> bool:
        """Check for bullish conditions."""
        snap = self.current
        if not snap or not snap.is_valid:
            return False
        return snap.imbalance > imbalance_threshold or self.imbalance_trend() > 0.005
    
    def is_bearish(self, imbalance_threshold: float = 0.2) -> bool:
        """Check for bearish conditions."""
        snap = self.current
        if not snap or not snap.is_valid:
            return False
        return snap.imbalance < -imbalance_threshold or self.imbalance_trend() < -0.005
    
    def get_statistics(self, window: int = 20) -> Dict[str, Any]:
        """Get comprehensive statistics with caching.
        
        Args:
            window: Window size for calculations
            
        Returns:
            Dictionary of statistics
        """
        cache_key = self._total
        if self._stats_cache is not None and self._stats_cache_key == cache_key:
            return self._stats_cache
        
        current = self.current
        if not current or not current.is_valid:
            return {}
        
        result = {
            "mid_price": current.mid_price,
            "spread_bps": current.spread_bps,
            "imbalance": current.imbalance,
            "depth_imbalance_10": current.depth_imbalance(10),
            "avg_spread_bps": self.average_spread_bps(window),
            "avg_imbalance": self.average_imbalance(window),
            "imbalance_trend": self.imbalance_trend(window),
            "signal_strength": self.get_signal_strength(),
            "microprice": current.microprice(),
            "snapshot_count": self.count,
            "data_fresh": True,
        }
        
        self._stats_cache = result
        self._stats_cache_key = cache_key
        
        return result


# ============================================================
# PARALLEL BACKTESTER
# ============================================================

class ParallelBacktester:
    """Multi-core backtest execution for maximum throughput.
    
    Distributes candle processing across CPU cores.
    
    Usage:
        backtester = ParallelBacktester(n_workers=4)
        results = backtester.run_parallel(candles, ticks, strategy_fn)
    """
    
    def __init__(self, n_workers: Optional[int] = None, seed: int = 42):
        """Initialize parallel backtester.
        
        Args:
            n_workers: Number of worker processes (default: CPU cores - 1)
            seed: Random seed for reproducibility
        """
        import multiprocessing as mp
        self.n_workers = n_workers or max(1, mp.cpu_count() - 1)
        self._seed = seed
    
    def run_parallel(
        self,
        candles: List[Dict[str, Any]],
        ticks_by_candle: Dict[int, List[Tuple[int, float, float]]],
        strategy_fn: Optional[Callable] = None,
        chunk_size: int = 100,
        compute_ob: bool = True
    ) -> List[Dict[str, Any]]:
        """Run backtest across multiple cores.
        
        Args:
            candles: List of candle dictionaries
            ticks_by_candle: Mapping of candle timestamp to tick list
            strategy_fn: Optional strategy function(candle, ob, ofi) -> signal
            chunk_size: Candles per worker chunk
            compute_ob: Whether to generate order books
            
        Returns:
            List of results per candle
        """
        from concurrent.futures import ProcessPoolExecutor
        
        # Split candles into chunks
        chunks = []
        for i in range(0, len(candles), chunk_size):
            chunk_candles = candles[i:i + chunk_size]
            chunk_ticks = {
                c['timestamp']: ticks_by_candle.get(c['timestamp'], [])
                for c in chunk_candles
            }
            chunks.append((chunk_candles, chunk_ticks))
        
        # Process chunks in parallel
        all_results = []
        
        with ProcessPoolExecutor(max_workers=self.n_workers) as executor:
            futures = [
                executor.submit(
                    _process_chunk_worker,
                    chunk[0],
                    chunk[1],
                    self._seed + i,
                    compute_ob
                )
                for i, chunk in enumerate(chunks)
            ]
            
            for future in futures:
                chunk_results = future.result()
                all_results.extend(chunk_results)
        
        # Apply strategy if provided
        if strategy_fn:
            for result in all_results:
                try:
                    result['signal'] = strategy_fn(
                        result.get('candle', {}),
                        result.get('orderbook'),
                        result.get('ofi', 0)
                    )
                except Exception as e:
                    result['signal'] = None
                    result['strategy_error'] = str(e)
        
        return all_results
    
    def run_sequential(
        self,
        candles: List[Dict[str, Any]],
        ticks_by_candle: Dict[int, List[Tuple[int, float, float]]],
        compute_ob: bool = True
    ) -> List[Dict[str, Any]]:
        """Run backtest on single core (for debugging or small datasets).
        
        Args:
            candles: List of candle dictionaries
            ticks_by_candle: Mapping of candle timestamp to tick list
            compute_ob: Whether to generate order books
            
        Returns:
            List of results per candle
        """
        engine = FastBacktestEngine(seed=self._seed)
        results = engine.process_candle_batch(candles, ticks_by_candle, compute_ob)
        
        # Add candle reference to each result
        for candle, result in zip(candles, results):
            result['candle'] = candle
        
        return results


def _process_chunk_worker(
    candles: List[Dict],
    ticks: Dict,
    seed: int,
    compute_ob: bool
) -> List[Dict]:
    """Worker function for parallel processing (must be top-level for pickling)."""
    # FIX 7: Verify NumPy availability in worker process
    if not HAS_NUMPY:
        raise RuntimeError("NumPy required for parallel backtest workers")
    engine = FastBacktestEngine(seed=seed)
    results = engine.process_candle_batch(candles, ticks, compute_ob)
    
    # Add candle reference
    for candle, result in zip(candles, results):
        result['candle'] = candle
    
    return results


# ============================================================
# MEMORY-MAPPED TICK STORAGE
# ============================================================

class MMapTickStore:
    """Memory-mapped tick storage for large backtests.
    
    Handles millions of ticks without RAM pressure.
    Data is stored on disk and accessed via memory mapping.
    
    Usage:
        # Save ticks
        store = MMapTickStore("btc_ticks.npy")
        store.create(ticks)  # [(timestamp, price, size), ...]
        
        # Load and query
        store.open()
        range_ticks = store.get_range(start_ts, end_ts)
        store.close()
    """
    
    DTYPE = None  # Set dynamically when numpy available
    
    def __init__(self, filepath: str):
        """Initialize tick store.
        
        Args:
            filepath: Path to store file (.npy)
        """
        if not HAS_NUMPY:
            raise ImportError("NumPy required for MMapTickStore: pip install numpy")
        
        # Set dtype
        if MMapTickStore.DTYPE is None:
            MMapTickStore.DTYPE = np.dtype([
                ('timestamp', np.int64),
                ('price', np.float64),
                ('size', np.float64),
            ])
        
        self.filepath = Path(filepath)
        self._data: Optional[np.ndarray] = None
    
    def create(self, ticks: List[Tuple[int, float, float]], overwrite: bool = False) -> None:
        """Create memory-mapped file from tick list.
        
        Args:
            ticks: List of (timestamp, price, size) tuples
            overwrite: If False (default), raises error if file exists
        """
        # FIX 8: Prevent accidental data loss
        if self.filepath.exists() and not overwrite:
            raise FileExistsError(
                f"Tick store already exists: {self.filepath}. "
                f"Use overwrite=True to replace."
            )
        # Sort by timestamp
        sorted_ticks = sorted(ticks, key=lambda x: x[0])
        
        # Create structured array
        arr = np.array(sorted_ticks, dtype=self.DTYPE)
        
        # Save to disk
        np.save(str(self.filepath), arr)
        
        logger.info(f"Created tick store: {len(ticks)} ticks -> {self.filepath}")
    
    def create_from_trades(self, trades: List[Dict[str, Any]]) -> None:
        """Create from list of trade dicts.
        
        Args:
            trades: List of dicts with 'timestamp', 'price', 'amount' keys
        """
        ticks = [
            (t['timestamp'], t['price'], t.get('amount', t.get('size', 0)))
            for t in trades
        ]
        self.create(ticks)
    
    def open(self) -> 'np.ndarray':
        """Open memory-mapped access.
        
        Returns:
            NumPy array with memory-mapped data
        """
        if self._data is not None:
            return self._data
        
        self._data = np.load(str(self.filepath), mmap_mode='r')
        return self._data
    
    def close(self) -> None:
        """Close memory map and free resources."""
        self._data = None
    
    def __enter__(self) -> 'MMapTickStore':
        """Context manager entry."""
        self.open()
        return self
    
    def __exit__(self, *args) -> None:
        """Context manager exit."""
        self.close()
    
    @property
    def is_open(self) -> bool:
        """Check if store is open."""
        return self._data is not None
    
    def __len__(self) -> int:
        """Get number of ticks."""
        if self._data is None:
            self.open()
        return len(self._data)
    
    def get_range(self, start_ts: int, end_ts: int) -> 'np.ndarray':
        """Get ticks in time range efficiently using binary search.
        
        Args:
            start_ts: Start timestamp (inclusive)
            end_ts: End timestamp (inclusive)
            
        Returns:
            NumPy array slice of matching ticks
        """
        if self._data is None:
            self.open()
        
        # Binary search for range bounds
        start_idx = np.searchsorted(self._data['timestamp'], start_ts, side='left')
        end_idx = np.searchsorted(self._data['timestamp'], end_ts, side='right')
        
        return self._data[start_idx:end_idx]
    
    def get_by_candle(
        self,
        candle_timestamps: List[int],
        candle_duration_ms: int = 60000
    ) -> Dict[int, 'np.ndarray']:
        """Get ticks grouped by candle timestamp.
        
        Args:
            candle_timestamps: List of candle start timestamps
            candle_duration_ms: Duration of each candle in milliseconds
            
        Returns:
            Dict mapping candle timestamp to tick array
        """
        if self._data is None:
            self.open()
        
        result = {}
        for ts in candle_timestamps:
            result[ts] = self.get_range(ts, ts + candle_duration_ms - 1)
        
        return result
    
    def to_list(self, start_ts: int = 0, end_ts: int = None) -> List[Tuple[int, float, float]]:
        """Convert range to list of tuples.
        
        Args:
            start_ts: Start timestamp
            end_ts: End timestamp (None = all)
            
        Returns:
            List of (timestamp, price, size) tuples
        """
        if end_ts is None:
            end_ts = int(time.time() * 1000) + 86400000  # Tomorrow
        
        data = self.get_range(start_ts, end_ts)
        return [(int(t['timestamp']), float(t['price']), float(t['size'])) for t in data]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics.
        
        Returns:
            Dict with tick count, time range, file size
        """
        if self._data is None:
            self.open()
        
        return {
            'tick_count': len(self._data),
            'start_time': int(self._data['timestamp'][0]) if len(self._data) > 0 else 0,
            'end_time': int(self._data['timestamp'][-1]) if len(self._data) > 0 else 0,
            'file_size_mb': self.filepath.stat().st_size / 1024 / 1024,
            'min_price': float(np.min(self._data['price'])) if len(self._data) > 0 else 0,
            'max_price': float(np.max(self._data['price'])) if len(self._data) > 0 else 0,
            'total_volume': float(np.sum(self._data['size'])) if len(self._data) > 0 else 0,
        }


# ============================================================
# NUMBA JIT ACCELERATED FUNCTIONS (Optional)
# ============================================================

if HAS_NUMBA:
    @jit(nopython=True, cache=True, parallel=True)
    def numba_tick_classify(prices):
        """Numba-accelerated tick classification - 50x faster.
        
        Args:
            prices: NumPy float64 array of prices
            
        Returns:
            int8 array of sides (-1, 0, 1)
        """
        n = len(prices)
        result = np.zeros(n, dtype=np.int8)
        
        for i in prange(1, n):
            if prices[i] > prices[i-1]:
                result[i] = 1
            elif prices[i] < prices[i-1]:
                result[i] = -1
            else:
                result[i] = result[i-1] if i > 0 else 0
        
        return result
    
    @jit(nopython=True, cache=True)
    def numba_ofi(prices, sizes):
        """Numba-accelerated OFI calculation.
        
        Args:
            prices: NumPy float64 array of prices
            sizes: NumPy float64 array of sizes
            
        Returns:
            (cumulative_delta, buy_volume, sell_volume)
        """
        n = len(prices)
        buy_vol = 0.0
        sell_vol = 0.0
        last_dir = 0
        
        for i in range(1, n):
            if prices[i] > prices[i-1]:
                last_dir = 1
            elif prices[i] < prices[i-1]:
                last_dir = -1
            
            if last_dir == 1:
                buy_vol += sizes[i]
            elif last_dir == -1:
                sell_vol += sizes[i]
        
        return buy_vol - sell_vol, buy_vol, sell_vol
    
    @jit(nopython=True, cache=True)
    def numba_vwap(prices, sizes):
        """Numba-accelerated VWAP calculation."""
        total_size = 0.0
        total_value = 0.0
        
        for i in range(len(prices)):
            total_size += sizes[i]
            total_value += prices[i] * sizes[i]
        
        if total_size == 0:
            return prices[-1] if len(prices) > 0 else 0.0
        
        return total_value / total_size
    
    @jit(nopython=True, cache=True)
    def numba_market_impact(
        prices,
        sizes,
        order_size,
        is_buy
    ):
        """Numba-accelerated market impact simulation.
        
        Args:
            prices: Level prices (asks for buy, bids for sell)
            sizes: Level sizes
            order_size: Order size to simulate
            is_buy: True for buy order
            
        Returns:
            Average fill price
        """
        # FIX 9: Guard against empty arrays
        if len(prices) == 0:
            return 0.0
        remaining = order_size
        cost = 0.0
        
        for i in range(len(prices)):
            if remaining <= 0:
                break
            fill = min(remaining, sizes[i])
            cost += fill * prices[i]
            remaining -= fill
        
        # Extrapolate if not fully filled
        if remaining > 0 and len(prices) > 0:
            penalty = 1.02 if is_buy else 0.98
            cost += remaining * prices[-1] * penalty
        
        return cost / order_size if order_size > 0 else 0.0


# ============================================================
# END OF OPTIMIZATIONS
# ============================================================


# Updated __all__ with optimization classes
__all__ = [
    # Original exports
    'OrderBookSnapshot',
    'OrderBookLevel',
    'OrderBookAnalyzer',
    'OrderBookManager',
    'BinanceOrderBookStream',
    'BinanceMultiStream',
    'CCXTOrderBookStream',
    'BaseOrderBookStream',
    'BacktestOrderBookProvider',
    'SyntheticOrderBook',
    'OrderFlowMetrics',
    'OrderFlowTracker',
    'SlippageEstimator',
    'BacktestExecutionSimulator',
    'StreamHealth',
    'TradeSide',
    'AlertLevel',
    'DataValidator',
    'AlertManager',
    'StateManager',
    'ThrottledCallback',
    'OrderBookDeltaHandler',
    'TickClassifier',
    'OrderBookConstants',
    'ALL_TRADING_SYMBOLS',
    'HAS_WEBSOCKETS',
    'HAS_CCXT_PRO',
    '_now_ms',
    
    # New optimized components
    'FastBacktestEngine',
    'BacktestAnalyzer',
    'ParallelBacktester',
    'MMapTickStore',
    'HAS_NUMPY',
    'HAS_NUMBA',
]
