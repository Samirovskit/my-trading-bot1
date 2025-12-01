#!/usr/bin/env python3
"""
oracle_backtest.py - ORACLE BACKTEST ENGINE v9.0 (ULTIMATE EDITION)

FEATURES:
- Multi-pair parallel testing (80+ pairs)
- Multi-timeframe optimization (1m to 1d)
- Auto-scan modes: ALL, TOP_WINNERS, TOP_LOSERS, BOTH
- Parameter grid search with walk-forward validation
- Combination ranking: Symbol × Timeframe × Strategy × Parameters
- Top-N extraction (e.g., Top 40 combinations)
- Persistent caching with intelligent invalidation
- Resume capability for long runs
- Memory-efficient streaming processing

USAGE:
    # Manual single pair test
    python oracle_backtest.py --symbol BTC/USDT --timeframes 5m,15m,1h --days 14
    
    # Auto-scan all pairs
    python oracle_backtest.py --scan all --timeframes 5m,15m --days 7 --top 40
    
    # Scan only top winners/losers from previous run
    python oracle_backtest.py --scan winners --top-n 20 --days 7
    python oracle_backtest.py --scan losers --top-n 20 --days 7
    python oracle_backtest.py --scan both --top-n 20 --days 7
    
    # Periodic auto-scan (every 6 hours)
    python oracle_backtest.py --scan all --periodic 6h --days 7

Author: Oracle Trading System
Version: 9.0.0
"""

import warnings
warnings.filterwarnings("ignore")

import sys
import os
import time
import json
import asyncio
import pickle
import gzip
import hashlib
import logging
import argparse
import signal
import threading
import traceback
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional, Tuple, Set, Callable, Union, Iterator
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from functools import lru_cache
from enum import Enum
from itertools import product
import heapq

import pandas as pd
import numpy as np

# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================
# DEPENDENCY CHECK
# ============================================================

try:
    import ccxt
    import ccxt.async_support as ccxt_async
except ImportError:
    logger.error("❌ ccxt not installed. Run: pip install ccxt")
    sys.exit(1)

# ============================================================
# INTEGRATION IMPORTS
# ============================================================

STRATEGIES_LOADED = False
ORDERBOOK_LOADED = False

try:
    from oracle_strategies import (
        StrategyConfig,
        StrategyManager,
        TradeSignal,
        TradeAction,
        BandData,
        TickAnalysis,
        ALL_STRATEGY_CODES,
        STRATEGY_CLASSES,
        parse_strategy_filter,
        StrategyPerformance,
        PrecomputedMetrics,
        MarketRegime,
        PARAM_BOUNDS,
    )
    STRATEGIES_LOADED = True
except ImportError as e:
    logger.error(f"❌ oracle_strategies.py: {e}")

try:
    from oracle_orderbook import (
        OrderBookSnapshot,
        OrderBookAnalyzer,
        SyntheticOrderBook,
        BacktestAnalyzer,
        FastBacktestEngine,
        BacktestExecutionSimulator,
        HAS_NUMPY,
    )
    ORDERBOOK_LOADED = True
except ImportError as e:
    logger.warning(f"⚠️ oracle_orderbook.py: {e}")
    BacktestAnalyzer = None
    FastBacktestEngine = None
    HAS_NUMPY = False


# ============================================================
# CONSTANTS
# ============================================================

class ScanMode(Enum):
    """Auto-scan modes."""
    ALL = "all"
    WINNERS = "winners"
    LOSERS = "losers"
    BOTH = "both"
    MANUAL = "manual"


class BacktestConfig:
    """Immutable configuration constants."""
    
    # Parallel processing
    MAX_WORKERS: int = min(8, (os.cpu_count() or 4) - 1)
    ASYNC_CONCURRENCY: int = 10
    
    # Memory limits
    MAX_TRADES_CACHE: int = 500_000
    CHUNK_SIZE: int = 5000
    
    # Cache settings
    CACHE_VERSION: str = "v9"
    CACHE_RETENTION_DAYS: int = 14
    
    # Validation
    MIN_TRADES_FOR_RANKING: int = 20
    MIN_CANDLES: int = 100
    WARMUP_CANDLES: int = 50
    
    # Ranking
    DEFAULT_TOP_N: int = 40
    
    # Walk-forward
    TRAIN_RATIO: float = 0.7
    
    # Timeframes (ms)
    TIMEFRAME_MS: Dict[str, int] = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }
    
    # All tradeable symbols
    ALL_SYMBOLS: List[str] = [
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
# DATA CLASSES
# ============================================================

@dataclass(frozen=True, slots=True)
class CombinationKey:
    """Unique identifier for a Symbol × Timeframe × Strategy combination."""
    symbol: str
    timeframe: str
    strategy: str
    
    def __hash__(self):
        return hash((self.symbol, self.timeframe, self.strategy))
    
    def __str__(self):
        return f"{self.symbol}|{self.timeframe}|{self.strategy}"
    
    @classmethod
    def from_string(cls, s: str) -> 'CombinationKey':
        parts = s.split("|")
        return cls(parts[0], parts[1], parts[2])


@dataclass
class CombinationResult:
    """Complete result for a single combination."""
    key: CombinationKey
    
    # Performance metrics
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    
    # Timing
    avg_hold_time_ms: float = 0.0
    
    # Quality metrics
    profit_factor: float = 0.0
    expectancy: float = 0.0
    
    # Walk-forward
    train_pnl: float = 0.0
    test_pnl: float = 0.0
    overfit_ratio: float = 0.0  # test_pnl / train_pnl (1.0 = perfect, <0.5 = overfit)
    
    # Regime breakdown
    regime_pnl: Dict[str, float] = field(default_factory=dict)
    
    # Metadata
    last_updated: float = 0.0
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0
    
    @property
    def score(self) -> float:
        """
        Composite ranking score.
        
        Formula: 
            40% Total PnL (normalized)
            25% Sharpe Ratio
            20% Walk-Forward Stability (overfit_ratio)
            15% Profit Factor
        """
        if self.trades < BacktestConfig.MIN_TRADES_FOR_RANKING:
            return -9999.0
        
        # Normalize PnL to ~±1 range
        pnl_norm = np.tanh(self.total_pnl / 500)  # $500 = ~0.46
        
        # Normalize Sharpe
        sharpe_norm = np.tanh(self.sharpe / 2)  # Sharpe 2 = ~0.76
        
        # Walk-forward stability
        wf_norm = min(1.0, self.overfit_ratio) if self.overfit_ratio > 0 else 0.0
        
        # Profit factor (capped)
        pf_norm = min(1.0, (self.profit_factor - 1) / 2) if self.profit_factor > 1 else 0.0
        
        return (
            pnl_norm * 0.40 +
            sharpe_norm * 0.25 +
            wf_norm * 0.20 +
            pf_norm * 0.15
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.key.symbol,
            "timeframe": self.key.timeframe,
            "strategy": self.key.strategy,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "profit_factor": round(self.profit_factor, 2),
            "sharpe": round(self.sharpe, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "expectancy": round(self.expectancy, 6),
            "overfit_ratio": round(self.overfit_ratio, 2),
            "score": round(self.score, 4),
            "regime_breakdown": self.regime_pnl,
        }


@dataclass
class RankingEntry:
    """Entry in the top-N ranking heap."""
    score: float
    result: CombinationResult
    
    def __lt__(self, other):
        """Min-heap: lower score gets pushed out first."""
        return self.score < other.score


@dataclass
class ScanState:
    """Persistent state for periodic scans."""
    last_scan_time: float = 0.0
    last_scan_mode: str = "all"
    previous_winners: List[str] = field(default_factory=list)  # CombinationKey strings
    previous_losers: List[str] = field(default_factory=list)
    all_results: Dict[str, Dict] = field(default_factory=dict)  # key_str -> result dict
    
    def save(self, path: Path) -> None:
        with gzip.open(path, 'wt') as f:
            json.dump(asdict(self), f)
    
    @classmethod
    def load(cls, path: Path) -> Optional['ScanState']:
        if not path.exists():
            return None
        try:
            with gzip.open(path, 'rt') as f:
                data = json.load(f)
            return cls(**data)
        except Exception:
            return None


# ============================================================
# ENHANCED CACHE SYSTEM
# ============================================================

class CacheManager:
    """
    Multi-tier intelligent caching system.
    
    Features:
    - RAM disk preference for speed
    - Content-hash based invalidation
    - Automatic cleanup of stale data
    - Streaming large datasets
    """
    
    def __init__(self):
        self.base_dir = self._find_cache_dir()
        
        # Sub-directories
        self.trades_dir = self.base_dir / "trades"
        self.candles_dir = self.base_dir / "candles"
        self.results_dir = self.base_dir / "results"
        self.state_dir = self.base_dir / "state"
        
        for d in [self.trades_dir, self.candles_dir, self.results_dir, self.state_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        self._cleanup_old_cache()
        
        logger.info(f"📂 Cache initialized: {self.base_dir}")
    
    def _find_cache_dir(self) -> Path:
        """Try RAM disk first."""
        candidates = [
            Path("/dev/shm/oracle_bt_v9"),
            Path("/tmp/oracle_bt_v9"),
            Path.home() / ".cache" / "oracle_bt_v9",
            Path("cache_backtest_v9"),
        ]
        
        for path in candidates:
            try:
                path.mkdir(parents=True, exist_ok=True)
                (path / ".test").write_text("test")
                (path / ".test").unlink()
                return path
            except Exception:
                continue
        
        fallback = Path("cache_backtest_v9")
        fallback.mkdir(exist_ok=True)
        return fallback
    
    def _cleanup_old_cache(self) -> None:
        """Remove files older than retention period."""
        cutoff = time.time() - (BacktestConfig.CACHE_RETENTION_DAYS * 86400)
        
        for cache_dir in [self.trades_dir, self.candles_dir]:
            try:
                for f in cache_dir.glob("*.pkl.gz"):
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
            except Exception:
                pass
    
    @staticmethod
    def _safe_name(s: str) -> str:
        return s.replace("/", "_").replace(":", "_").replace(" ", "_")
    
    def _content_hash(self, data: Any) -> str:
        """Create hash of data content for invalidation."""
        if isinstance(data, list) and len(data) > 0:
            # For trades: hash first, last, length
            first = str(data[0])[:100]
            last = str(data[-1])[:100]
            return hashlib.md5(f"{len(data)}|{first}|{last}".encode()).hexdigest()[:12]
        return hashlib.md5(str(data).encode()).hexdigest()[:12]
    
    # ==================== TRADES ====================
    
    def get_trades_path(self, symbol: str) -> Path:
        return self.trades_dir / f"{self._safe_name(symbol)}_{BacktestConfig.CACHE_VERSION}.pkl.gz"
    
    def load_trades(self, symbol: str, start_ms: int) -> Tuple[List[Dict], int]:
        """Load cached trades. Returns (trades, last_timestamp)."""
        path = self.get_trades_path(symbol)
        if not path.exists():
            return [], start_ms
        
        try:
            with gzip.open(path, "rb") as f:
                trades = pickle.load(f)
            
            filtered = [t for t in trades if t['timestamp'] >= start_ms]
            last_ts = trades[-1]['timestamp'] if trades else start_ms
            
            return filtered, last_ts
        except Exception as e:
            logger.warning(f"Cache load failed {symbol}: {e}")
            return [], start_ms
    
    def save_trades(self, symbol: str, trades: List[Dict]) -> None:
        """Save trades with deduplication."""
        if not trades:
            return
        
        path = self.get_trades_path(symbol)
        
        try:
            existing, _ = self.load_trades(symbol, 0)
            
            # Merge and deduplicate
            seen = set()
            merged = []
            
            for t in existing + trades:
                key = (t['timestamp'], t['price'], t.get('amount', 0))
                if key not in seen:
                    seen.add(key)
                    merged.append(t)
            
            merged.sort(key=lambda x: x['timestamp'])
            
            # Limit size
            if len(merged) > BacktestConfig.MAX_TRADES_CACHE:
                merged = merged[-BacktestConfig.MAX_TRADES_CACHE:]
            
            with gzip.open(path, "wb") as f:
                pickle.dump(merged, f, protocol=pickle.HIGHEST_PROTOCOL)
                
        except Exception as e:
            logger.error(f"Cache save failed {symbol}: {e}")
    
    # ==================== CANDLES ====================
    
    def get_candles_path(self, symbol: str, timeframe: str) -> Path:
        return self.candles_dir / f"{self._safe_name(symbol)}_{timeframe}_{BacktestConfig.CACHE_VERSION}.pkl.gz"
    
    def load_candles(self, symbol: str, timeframe: str, 
                     max_age_seconds: int = 300) -> Optional[Tuple[pd.DataFrame, Dict]]:
        """Load cached candles if fresh."""
        path = self.get_candles_path(symbol, timeframe)
        if not path.exists():
            return None
        
        try:
            if time.time() - path.stat().st_mtime > max_age_seconds:
                return None
            
            with gzip.open(path, "rb") as f:
                data = pickle.load(f)
            
            return data["candles"], data["ticks_map"]
        except Exception:
            return None
    
    def save_candles(self, symbol: str, timeframe: str,
                     candles: pd.DataFrame, ticks_map: Dict) -> None:
        path = self.get_candles_path(symbol, timeframe)
        try:
            with gzip.open(path, "wb") as f:
                pickle.dump({
                    "candles": candles,
                    "ticks_map": ticks_map,
                }, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            logger.warning(f"Candle cache save failed: {e}")
    
    # ==================== RESULTS ====================
    
    def get_results_path(self, run_id: str) -> Path:
        return self.results_dir / f"{run_id}.pkl.gz"
    
    def save_results(self, run_id: str, results: Dict[str, CombinationResult]) -> None:
        path = self.get_results_path(run_id)
        try:
            serialized = {k: v.to_dict() for k, v in results.items()}
            with gzip.open(path, "wb") as f:
                pickle.dump(serialized, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            logger.error(f"Results save failed: {e}")
    
    def load_results(self, run_id: str) -> Optional[Dict[str, Dict]]:
        path = self.get_results_path(run_id)
        if not path.exists():
            return None
        
        try:
            with gzip.open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    
    # ==================== SCAN STATE ====================
    
    def get_scan_state_path(self) -> Path:
        return self.state_dir / "scan_state.json.gz"
    
    def load_scan_state(self) -> Optional[ScanState]:
        return ScanState.load(self.get_scan_state_path())
    
    def save_scan_state(self, state: ScanState) -> None:
        state.save(self.get_scan_state_path())


# Global cache
CACHE = CacheManager()


# ============================================================
# ASYNC DATA FETCHER
# ============================================================

class DataFetcher:
    """High-performance async data fetcher."""
    
    def __init__(self):
        self._exchange = None
        self._semaphore = asyncio.Semaphore(BacktestConfig.ASYNC_CONCURRENCY)
        self._rate_limiter = asyncio.Lock()
        self._last_request = 0.0
    
    async def _get_exchange(self):
        if self._exchange is None:
            self._exchange = ccxt_async.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
        return self._exchange
    
    async def _rate_limit(self):
        async with self._rate_limiter:
            now = time.time()
            if now - self._last_request < 0.1:
                await asyncio.sleep(0.1 - (now - self._last_request))
            self._last_request = time.time()
    
    async def fetch_trades(self, symbol: str, start_ts: int, end_ts: int,
                          progress_fn: Optional[Callable] = None) -> List[Dict]:
        """Fetch all trades in range."""
        all_trades = []
        cursor = start_ts
        
        while cursor < end_ts:
            async with self._semaphore:
                await self._rate_limit()
                
                try:
                    exchange = await self._get_exchange()
                    trades = await exchange.fetch_trades(symbol, since=cursor, limit=1000)
                except Exception as e:
                    logger.warning(f"Fetch error {symbol}: {e}")
                    await asyncio.sleep(1)
                    cursor += 60000
                    continue
            
            if not trades:
                cursor += 60000
                continue
            
            valid = [t for t in trades if start_ts <= t['timestamp'] <= end_ts]
            all_trades.extend(valid)
            
            last_ts = trades[-1]['timestamp']
            cursor = last_ts + 1 if last_ts > cursor else cursor + 1000
            
            if progress_fn and len(all_trades) % 10000 == 0:
                progress = min(100, (cursor - start_ts) / (end_ts - start_ts) * 100)
                progress_fn(symbol, progress, len(all_trades))
            
            if cursor >= end_ts:
                break
        
        return all_trades
    
    async def close(self):
        if self._exchange:
            await self._exchange.close()
            self._exchange = None


# ============================================================
# CANDLE BUILDER
# ============================================================

class CandleBuilder:
    """Optimized candle construction with tick preservation."""
    
    @staticmethod
    def build(trades: List[Dict], timeframe_ms: int) -> Tuple[pd.DataFrame, Dict[int, List]]:
        if not trades:
            return pd.DataFrame(), {}
        
        n = len(trades)
        ts = np.array([t['timestamp'] for t in trades], dtype=np.int64)
        prices = np.array([t['price'] for t in trades], dtype=np.float64)
        volumes = np.array([t['amount'] for t in trades], dtype=np.float64)
        
        buckets = (ts // timeframe_ms) * timeframe_ms
        unique_buckets = np.unique(buckets)
        
        n_candles = len(unique_buckets)
        opens = np.empty(n_candles, dtype=np.float64)
        highs = np.empty(n_candles, dtype=np.float64)
        lows = np.empty(n_candles, dtype=np.float64)
        closes = np.empty(n_candles, dtype=np.float64)
        vols = np.empty(n_candles, dtype=np.float64)
        candle_ts = np.empty(n_candles, dtype=np.int64)
        
        ticks_map: Dict[int, List[Tuple[int, float, float]]] = {}
        
        for i, bucket in enumerate(unique_buckets):
            mask = buckets == bucket
            bp = prices[mask]
            bv = volumes[mask]
            bt = ts[mask]
            
            opens[i] = bp[0]
            highs[i] = np.max(bp)
            lows[i] = np.min(bp)
            closes[i] = bp[-1]
            vols[i] = np.sum(bv)
            candle_ts[i] = bucket
            
            ticks_map[int(bucket)] = [
                (int(t), float(p), float(v))
                for t, p, v in zip(bt, bp, bv)
            ]
        
        candles = pd.DataFrame({
            'timestamp': candle_ts,
            'open': opens,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': vols,
        })
        
        return candles, ticks_map


# ============================================================
# BAND PREDICTOR
# ============================================================

class BandPredictor:
    """Adaptive Bollinger band prediction."""
    
    @staticmethod
    def predict(candles: pd.DataFrame, current_idx: int) -> Tuple[float, float]:
        if current_idx < 20:
            return 0.0, 0.0
        
        history = candles.iloc[max(0, current_idx - 50):current_idx]
        closes = history['close'].values
        
        if len(closes) < 20:
            return 0.0, 0.0
        
        sma = np.mean(closes[-20:])
        std = np.std(closes[-20:])
        
        # Adaptive multiplier
        recent_vol = np.std(closes[-5:]) if len(closes) >= 5 else std
        vol_ratio = recent_vol / std if std > 0 else 1.0
        multiplier = 2.0 * (0.8 + 0.4 * vol_ratio)
        multiplier = min(3.0, max(1.5, multiplier))
        
        band_low = sma - (multiplier * std)
        band_high = sma + (multiplier * std)
        
        # Sanity bounds
        current = closes[-1]
        band_low = max(band_low, current * 0.95)
        band_high = min(band_high, current * 1.05)
        
        return band_low, band_high


# ============================================================
# EXECUTION SIMULATOR
# ============================================================

class Simulator:
    """Realistic execution simulation."""
    
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.fee = config.fee
        self.slippage = config.slippage
    
    def execute(self, signal: TradeSignal, ticks: List[Tuple[int, float, float]],
                capital: float, candle_volume: float) -> Optional[Dict]:
        if not ticks or not signal or not signal.is_valid():
            return None
        
        is_long = signal.action == TradeAction.LONG
        entry_target = signal.entry_price
        exit_target = signal.exit_price
        stop_loss = signal.stop_loss
        position_value = capital * signal.position_size
        
        # Volume-based slippage
        slip = self.slippage
        if candle_volume > 0:
            impact = min(1.0, (position_value / candle_volume) ** 0.5)
            slip += impact * 0.001
        slip = min(slip, 0.005)
        
        in_position = False
        entry_price = 0.0
        entry_ts = 0
        
        for ts, price, size in ticks:
            if not in_position:
                if is_long and price <= entry_target:
                    entry_price = entry_target * (1 + slip)
                    entry_ts = ts
                    in_position = True
                elif not is_long and price >= entry_target:
                    entry_price = entry_target * (1 - slip)
                    entry_ts = ts
                    in_position = True
                continue
            
            exit_price = None
            exit_reason = None
            
            # Stop loss
            if stop_loss:
                if is_long and price <= stop_loss:
                    exit_price = stop_loss * (1 - slip)
                    exit_reason = "stop_loss"
                elif not is_long and price >= stop_loss:
                    exit_price = stop_loss * (1 + slip)
                    exit_reason = "stop_loss"
            
            # Take profit
            if exit_price is None:
                if is_long and price >= exit_target:
                    exit_price = exit_target * (1 - slip)
                    exit_reason = "take_profit"
                elif not is_long and price <= exit_target:
                    exit_price = exit_target * (1 + slip)
                    exit_reason = "take_profit"
            
            if exit_price:
                return self._calc_result(entry_price, exit_price, entry_ts, ts,
                                        is_long, position_value, exit_reason)
        
        # Force close at end
        if in_position and ticks:
            last_price, last_ts = ticks[-1][1], ticks[-1][0]
            exit_price = last_price * (1 - slip if is_long else 1 + slip)
            return self._calc_result(entry_price, exit_price, entry_ts, last_ts,
                                    is_long, position_value, "candle_close")
        
        return None
    
    def _calc_result(self, entry: float, exit: float, entry_ts: int, exit_ts: int,
                     is_long: bool, position_value: float, reason: str) -> Dict:
        if is_long:
            gross_return = (exit - entry) / entry
        else:
            gross_return = (entry - exit) / entry
        
        gross_pnl = position_value * gross_return
        fees = position_value * self.fee * 2
        net_pnl = gross_pnl - fees
        
        return {
            "entry_price": entry,
            "exit_price": exit,
            "hold_time_ms": exit_ts - entry_ts,
            "gross_return": gross_return,
            "net_pnl": net_pnl,
            "fees": fees,
            "exit_reason": reason,
        }


# ============================================================
# COMBINATION BACKTESTER
# ============================================================

class CombinationBacktester:
    """
    Backtest a single Symbol × Timeframe combination.
    
    Returns per-strategy results for ranking.
    """
    
    def __init__(self, symbol: str, timeframe: str, config: StrategyConfig,
                 strategy_codes: List[str], capital: float = 10000.0):
        self.symbol = symbol
        self.timeframe = timeframe
        self.config = config
        self.strategy_codes = strategy_codes
        self.initial_capital = capital
        
        self.simulator = Simulator(config)
        self.manager = StrategyManager(config, strategy_codes)
        
        # Per-strategy tracking
        self.strategy_results: Dict[str, CombinationResult] = {}
        self.strategy_pnl_series: Dict[str, List[float]] = {}
        self.strategy_equity: Dict[str, List[float]] = {}
        
        for code in strategy_codes:
            key = CombinationKey(symbol, timeframe, code)
            self.strategy_results[code] = CombinationResult(key=key)
            self.strategy_pnl_series[code] = []
            self.strategy_equity[code] = [capital]
    
    def run(self, candles: pd.DataFrame, ticks_map: Dict[int, List],
            warmup: int = 50) -> Dict[str, CombinationResult]:
        """Run backtest and return per-strategy results."""
        if len(candles) < warmup + 10:
            return self.strategy_results
        
        n_candles = len(candles)
        train_end_idx = int(n_candles * BacktestConfig.TRAIN_RATIO)
        
        for i in range(warmup, n_candles):
            candle = candles.iloc[i]
            ts = int(candle['timestamp'])
            
            history = candles.iloc[max(0, i - 100):i]
            band_low, band_high = BandPredictor.predict(candles, i)
            
            if band_low <= 0 or band_high <= band_low:
                continue
            
            ticks = ticks_map.get(ts, [])
            if not ticks:
                continue
            
            # Build synthetic order book
            ob_analyzer = None
            if self.config.use_order_book and ORDERBOOK_LOADED and FastBacktestEngine:
                try:
                    engine = FastBacktestEngine(seed=ts % 10000)
                    ob = engine.fast_synthetic_ob(
                        last_price=ticks[-1][1],
                        last_timestamp=ts,
                        tick_imbalance=0.0,
                        spread_bps=5.0,
                        levels=10
                    )
                    if ob and ob.is_valid and BacktestAnalyzer:
                        ob_analyzer = BacktestAnalyzer(max_snapshots=1)
                        ob_analyzer.add(ob)
                except Exception:
                    pass
            
            # Evaluate strategies
            signals = self.manager.evaluate_all(
                band_low=band_low,
                band_high=band_high,
                ticks=[(t[0], t[1]) for t in ticks],
                candle_data=history,
                order_book=ob_analyzer,
                tick_sizes=[t[2] for t in ticks]
            )
            
            candle_volume = float(candle['volume'])
            is_train = i < train_end_idx
            
            # Execute each signal
            for code, signal in signals.items():
                if not signal or not signal.is_valid():
                    continue
                
                current_capital = self.strategy_equity[code][-1]
                result = self.simulator.execute(signal, ticks, current_capital, candle_volume)
                
                if result:
                    self._update_strategy(code, result, signal, is_train)
        
        # Finalize metrics
        self._finalize_metrics()
        
        return self.strategy_results
    
    def _update_strategy(self, code: str, result: Dict, signal: TradeSignal, is_train: bool):
        """Update strategy result with trade outcome."""
        cr = self.strategy_results[code]
        pnl = result['net_pnl']
        
        cr.trades += 1
        cr.total_pnl += pnl
        
        if pnl > 0:
            cr.wins += 1
            cr.gross_profit += pnl
        else:
            cr.gross_loss += pnl
        
        # Walk-forward split
        if is_train:
            cr.train_pnl += pnl
        else:
            cr.test_pnl += pnl
        
        # Regime tracking
        regime = signal.metadata.get('market_regime', 'unknown')
        cr.regime_pnl[regime] = cr.regime_pnl.get(regime, 0.0) + pnl
        
        # Equity tracking
        self.strategy_pnl_series[code].append(pnl)
        new_equity = self.strategy_equity[code][-1] + pnl
        self.strategy_equity[code].append(new_equity)
        
        # Avg hold time
        cr.avg_hold_time_ms = (
            (cr.avg_hold_time_ms * (cr.trades - 1) + result['hold_time_ms']) / cr.trades
        )
    
    def _finalize_metrics(self):
        """Calculate final metrics for all strategies."""
        for code, cr in self.strategy_results.items():
            if cr.trades == 0:
                continue
            
            # Profit factor
            if abs(cr.gross_loss) > 1e-10:
                cr.profit_factor = cr.gross_profit / abs(cr.gross_loss)
            else:
                cr.profit_factor = float('inf') if cr.gross_profit > 0 else 0.0
            
            # Expectancy
            avg_win = cr.gross_profit / cr.wins if cr.wins > 0 else 0
            avg_loss = cr.gross_loss / (cr.trades - cr.wins) if cr.trades > cr.wins else 0
            cr.expectancy = (cr.win_rate * avg_win) + ((1 - cr.win_rate) * avg_loss)
            
            # Sharpe
            pnl_series = self.strategy_pnl_series[code]
            if len(pnl_series) > 1:
                arr = np.array(pnl_series)
                if np.std(arr) > 0:
                    cr.sharpe = float(np.mean(arr) / np.std(arr) * np.sqrt(252))
            
            # Max drawdown
            equity = np.array(self.strategy_equity[code])
            peaks = np.maximum.accumulate(equity)
            drawdowns = (peaks - equity) / peaks
            cr.max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
            
            # Walk-forward overfit ratio
            if abs(cr.train_pnl) > 1e-10:
                cr.overfit_ratio = cr.test_pnl / cr.train_pnl
            else:
                cr.overfit_ratio = 0.0
            
            cr.last_updated = time.time()


# ============================================================
# RANKING SYSTEM
# ============================================================

class RankingSystem:
    """
    Maintains top-N combinations using a min-heap.
    
    Efficient O(log N) insertion.
    """
    
    def __init__(self, max_size: int = 40):
        self.max_size = max_size
        self._heap: List[RankingEntry] = []
        self._all_results: Dict[str, CombinationResult] = {}
    
    def add(self, result: CombinationResult) -> None:
        """Add result to ranking."""
        key_str = str(result.key)
        self._all_results[key_str] = result
        
        if result.trades < BacktestConfig.MIN_TRADES_FOR_RANKING:
            return
        
        entry = RankingEntry(score=result.score, result=result)
        
        if len(self._heap) < self.max_size:
            heapq.heappush(self._heap, entry)
        elif entry.score > self._heap[0].score:
            heapq.heapreplace(self._heap, entry)
    
    def get_top_n(self, n: Optional[int] = None) -> List[CombinationResult]:
        """Get top N results sorted by score descending."""
        n = n or self.max_size
        sorted_entries = sorted(self._heap, key=lambda x: x.score, reverse=True)
        return [e.result for e in sorted_entries[:n]]
    
    def get_all_results(self) -> Dict[str, CombinationResult]:
        return self._all_results
    
    def get_winners(self, n: int = 20) -> List[str]:
        """Get top N combination keys."""
        top = self.get_top_n(n)
        return [str(r.key) for r in top]
    
    def get_losers(self, n: int = 20) -> List[str]:
        """Get bottom N by PnL."""
        sorted_results = sorted(
            self._all_results.values(),
            key=lambda x: x.total_pnl
        )
        return [str(r.key) for r in sorted_results[:n]]
    
    def to_dataframe(self) -> pd.DataFrame:
        """Export ranking as DataFrame."""
        top = self.get_top_n()
        rows = []
        
        for rank, result in enumerate(top, 1):
            rows.append({
                "Rank": rank,
                "Symbol": result.key.symbol,
                "TF": result.key.timeframe,
                "Strategy": result.key.strategy,
                "Trades": result.trades,
                "WinRate%": round(result.win_rate * 100, 1),
                "TotalPnL": round(result.total_pnl, 2),
                "Sharpe": round(result.sharpe, 2),
                "PF": round(result.profit_factor, 2) if result.profit_factor < 999 else 999,
                "MaxDD%": round(result.max_drawdown * 100, 1),
                "WF_Ratio": round(result.overfit_ratio, 2),
                "Score": round(result.score, 4),
            })
        
        return pd.DataFrame(rows)


# ============================================================
# ORCHESTRATOR
# ============================================================

class BacktestOrchestrator:
    """
    Main orchestration engine.
    
    Handles:
    - Manual single-pair testing
    - Batch multi-pair testing
    - Auto-scan modes (all, winners, losers, both)
    - Periodic scheduled scans
    """
    
    def __init__(self, config: StrategyConfig, strategy_codes: List[str],
                 capital: float = 10000.0, top_n: int = 40):
        self.config = config
        self.strategy_codes = strategy_codes
        self.capital = capital
        self.top_n = top_n
        
        self.ranking = RankingSystem(max_size=top_n * 2)  # Keep extra for analysis
        self.scan_state = CACHE.load_scan_state() or ScanState()
        
        self._stop_requested = False
    
    def _signal_handler(self, signum, frame):
        logger.info("🛑 Stop requested, finishing current batch...")
        self._stop_requested = True
    
    async def _fetch_data(self, symbol: str, start_ts: int, end_ts: int) -> List[Dict]:
        """Fetch or load cached trades."""
        cached, last_ts = CACHE.load_trades(symbol, start_ts)
        
        if last_ts >= end_ts - 60000:
            return cached
        
        fetcher = DataFetcher()
        try:
            new_trades = await fetcher.fetch_trades(symbol, max(last_ts, start_ts), end_ts)
            all_trades = cached + new_trades
            CACHE.save_trades(symbol, all_trades)
            return all_trades
        finally:
            await fetcher.close()
    
    def _process_symbol_timeframe(self, symbol: str, timeframe: str,
                                  trades: List[Dict]) -> Dict[str, CombinationResult]:
        """Process single symbol/timeframe combination."""
        tf_ms = BacktestConfig.TIMEFRAME_MS.get(timeframe, 300_000)
        
        # Try cache first
        cached = CACHE.load_candles(symbol, timeframe, max_age_seconds=300)
        if cached:
            candles, ticks_map = cached
        else:
            candles, ticks_map = CandleBuilder.build(trades, tf_ms)
            if not candles.empty:
                CACHE.save_candles(symbol, timeframe, candles, ticks_map)
        
        if candles.empty or len(candles) < BacktestConfig.MIN_CANDLES:
            return {}
        
        # Run backtest
        backtester = CombinationBacktester(
            symbol=symbol,
            timeframe=timeframe,
            config=self.config,
            strategy_codes=self.strategy_codes,
            capital=self.capital
        )
        
        return backtester.run(candles, ticks_map, warmup=BacktestConfig.WARMUP_CANDLES)
    
    async def run_manual(self, symbols: List[str], timeframes: List[str],
                         days: int = 7) -> RankingSystem:
        """Run manual backtest on specified symbols and timeframes."""
        logger.info(f"🎯 Manual backtest: {len(symbols)} symbols × {len(timeframes)} timeframes")
        
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - (days * 24 * 60 * 60 * 1000)
        
        total_combos = len(symbols) * len(timeframes)
        completed = 0
        
        for symbol in symbols:
            if self._stop_requested:
                break
            
            # Fetch data once per symbol
            trades = await self._fetch_data(symbol, start_ts, end_ts)
            
            if not trades:
                logger.warning(f"   ⚠️ No trades for {symbol}")
                continue
            
            for tf in timeframes:
                if self._stop_requested:
                    break
                
                try:
                    results = self._process_symbol_timeframe(symbol, tf, trades)
                    
                    for code, result in results.items():
                        self.ranking.add(result)
                    
                    completed += 1
                    progress = completed / total_combos * 100
                    
                    if completed % 10 == 0:
                        logger.info(f"   [{progress:5.1f}%] {symbol} {tf} complete")
                        
                except Exception as e:
                    logger.error(f"   ❌ {symbol} {tf}: {e}")
        
        return self.ranking
    
    async def run_scan(self, mode: ScanMode, timeframes: List[str],
                       days: int = 7, top_n_filter: int = 20) -> RankingSystem:
        """
        Run auto-scan based on mode.
        
        Modes:
            ALL: Test all 80+ symbols
            WINNERS: Re-test previous top performers
            LOSERS: Re-test previous losers (looking for recovery)
            BOTH: Test both winners and losers
        """
        # Determine symbols to test
        if mode == ScanMode.ALL:
            symbols = BacktestConfig.ALL_SYMBOLS
            logger.info(f"🔍 Scanning ALL {len(symbols)} symbols")
            
        elif mode == ScanMode.WINNERS:
            if not self.scan_state.previous_winners:
                logger.warning("No previous winners. Falling back to ALL.")
                symbols = BacktestConfig.ALL_SYMBOLS
            else:
                # Extract unique symbols from winner keys
                symbols = list(set(
                    CombinationKey.from_string(k).symbol 
                    for k in self.scan_state.previous_winners[:top_n_filter]
                ))
                logger.info(f"🏆 Scanning {len(symbols)} previous winners")
                
        elif mode == ScanMode.LOSERS:
            if not self.scan_state.previous_losers:
                logger.warning("No previous losers. Falling back to ALL.")
                symbols = BacktestConfig.ALL_SYMBOLS
            else:
                symbols = list(set(
                    CombinationKey.from_string(k).symbol 
                    for k in self.scan_state.previous_losers[:top_n_filter]
                ))
                logger.info(f"📉 Scanning {len(symbols)} previous losers")
                
        elif mode == ScanMode.BOTH:
            winner_syms = set(
                CombinationKey.from_string(k).symbol 
                for k in self.scan_state.previous_winners[:top_n_filter]
            ) if self.scan_state.previous_winners else set()
            
            loser_syms = set(
                CombinationKey.from_string(k).symbol 
                for k in self.scan_state.previous_losers[:top_n_filter]
            ) if self.scan_state.previous_losers else set()
            
            symbols = list(winner_syms | loser_syms)
            
            if not symbols:
                logger.warning("No previous data. Falling back to ALL.")
                symbols = BacktestConfig.ALL_SYMBOLS
            else:
                logger.info(f"🔄 Scanning {len(symbols)} winners + losers")
        else:
            symbols = BacktestConfig.ALL_SYMBOLS
        
        # Run backtest
        ranking = await self.run_manual(symbols, timeframes, days)
        
        # Update scan state
        self.scan_state.last_scan_time = time.time()
        self.scan_state.last_scan_mode = mode.value
        self.scan_state.previous_winners = ranking.get_winners(top_n_filter)
        self.scan_state.previous_losers = ranking.get_losers(top_n_filter)
        self.scan_state.all_results = {
            k: v.to_dict() for k, v in ranking.get_all_results().items()
        }
        
        CACHE.save_scan_state(self.scan_state)
        
        return ranking
    
    async def run_periodic(self, mode: ScanMode, timeframes: List[str],
                           days: int, interval_hours: float) -> None:
        """Run periodic scans at specified interval."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        interval_seconds = interval_hours * 3600
        
        logger.info(f"⏰ Starting periodic scan: every {interval_hours}h")
        
        while not self._stop_requested:
            start_time = time.time()
            
            logger.info(f"\n{'='*60}")
            logger.info(f"🔄 Starting periodic scan: {datetime.now()}")
            logger.info(f"{'='*60}")
            
            ranking = await self.run_scan(mode, timeframes, days)
            
            # Print summary
            self._print_ranking_summary(ranking)
            
            elapsed = time.time() - start_time
            sleep_time = max(0, interval_seconds - elapsed)
            
            logger.info(f"😴 Next scan in {sleep_time/3600:.1f}h")
            
            # Sleep in small increments to allow interrupt
            while sleep_time > 0 and not self._stop_requested:
                await asyncio.sleep(min(60, sleep_time))
                sleep_time -= 60
    
    def _print_ranking_summary(self, ranking: RankingSystem) -> None:
        """Print formatted ranking summary."""
        df = ranking.to_dataframe()
        
        if df.empty:
            print("\n⚠️ No qualifying combinations found.")
            return
        
        print("\n" + "=" * 80)
        print(f"🏆 TOP {len(df)} COMBINATIONS (by Composite Score)")
        print("=" * 80)
        print(df.to_string(index=False))
        
        # Strategy summary
        strategy_totals = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "count": 0})
        
        for result in ranking.get_top_n():
            code = result.key.strategy
            strategy_totals[code]["trades"] += result.trades
            strategy_totals[code]["pnl"] += result.total_pnl
            strategy_totals[code]["count"] += 1
        
        print("\n" + "-" * 60)
        print("📊 STRATEGY PRESENCE IN TOP RANKINGS")
        print("-" * 60)
        
        sorted_strategies = sorted(
            strategy_totals.items(),
            key=lambda x: x[1]["pnl"],
            reverse=True
        )
        
        for code, data in sorted_strategies[:10]:
            print(f"   {code:<15} Appearances: {data['count']:>3}  Total PnL: ${data['pnl']:>10,.2f}")
        
        # Symbol distribution
        symbol_counts = defaultdict(int)
        for result in ranking.get_top_n():
            symbol_counts[result.key.symbol] += 1
        
        top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        
        print("\n" + "-" * 60)
        print("💹 SYMBOLS WITH MOST TOP COMBINATIONS")
        print("-" * 60)
        
        for symbol, count in top_symbols:
            print(f"   {symbol:<12} {count} combinations")
        
        print("=" * 80)


# ============================================================
# REPORT EXPORTER
# ============================================================

class ReportExporter:
    """Export results in various formats."""
    
    def __init__(self, output_dir: Path = Path("backtest_results")):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    
    def export_ranking_csv(self, ranking: RankingSystem, prefix: str = "ranking") -> Path:
        df = ranking.to_dataframe()
        path = self.output_dir / f"{prefix}_{self._timestamp()}.csv"
        df.to_csv(path, index=False)
        logger.info(f"💾 Exported ranking to {path}")
        return path
    
    def export_full_json(self, ranking: RankingSystem, prefix: str = "full") -> Path:
        data = {
            "timestamp": self._timestamp(),
            "top_n": [r.to_dict() for r in ranking.get_top_n()],
            "all_results": {k: v.to_dict() for k, v in ranking.get_all_results().items()},
        }
        
        path = self.output_dir / f"{prefix}_{self._timestamp()}.json"
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        
        logger.info(f"💾 Exported full results to {path}")
        return path
    
    def export_paper_trading_config(self, ranking: RankingSystem, 
                                    n: int = 10, prefix: str = "paper") -> Path:
        """Export config for paper trading top N combinations."""
        top = ranking.get_top_n(n)
        
        config = {
            "generated_at": datetime.now().isoformat(),
            "combinations": [
                {
                    "symbol": r.key.symbol,
                    "timeframe": r.key.timeframe,
                    "strategy": r.key.strategy,
                    "expected_winrate": round(r.win_rate, 4),
                    "expected_sharpe": round(r.sharpe, 2),
                    "backtest_pnl": round(r.total_pnl, 2),
                    "score": round(r.score, 4),
                }
                for r in top
            ]
        }
        
        path = self.output_dir / f"{prefix}_config_{self._timestamp()}.json"
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
        
        logger.info(f"💾 Exported paper trading config to {path}")
        return path


# ============================================================
# MAIN CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Oracle Backtest Engine v9.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Manual single pair
  python oracle_backtest.py --symbol BTC/USDT --timeframes 5m,15m,1h --days 7
  
  # Scan all 80+ pairs
  python oracle_backtest.py --scan all --timeframes 5m,15m --days 7 --top 40
  
  # Scan previous winners only (fast re-test)
  python oracle_backtest.py --scan winners --days 7
  
  # Scan both winners and losers
  python oracle_backtest.py --scan both --top-n 30 --days 14
  
  # Periodic auto-scan every 6 hours
  python oracle_backtest.py --scan all --periodic 6 --days 7
  
  # Specific strategies only
  python oracle_backtest.py --scan all --strategies S1,S2,S7,S12 --days 7
        """
    )
    
    # Mode selection
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--symbol", type=str, help="Single symbol (manual mode)")
    mode.add_argument("--symbols", type=str, help="Comma-separated symbols")
    mode.add_argument("--scan", type=str, choices=["all", "winners", "losers", "both"],
                      help="Auto-scan mode")
    
    # Time parameters
    parser.add_argument("--timeframes", "-tf", default="5m,15m,1h",
                        help="Comma-separated timeframes")
    parser.add_argument("--days", type=int, default=7, help="Days of history")
    
    # Strategy selection
    parser.add_argument("--strategies", "-s", default="all",
                        help="Strategy filter (S1,S2,S3 or S1-S11 or all)")
    
    # Ranking
    parser.add_argument("--top", type=int, default=40, help="Top N combinations to track")
    parser.add_argument("--top-n", type=int, default=20, 
                        help="Top N for winners/losers filter")
    
    # Periodic
    parser.add_argument("--periodic", type=float, help="Periodic scan interval (hours)")
    
    # Trading parameters
    parser.add_argument("--fee", type=float, default=0.0004)
    parser.add_argument("--slippage", type=float, default=0.0005)
    parser.add_argument("--pos-size", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--min-band", type=float, default=0.15)
    
    # Options
    parser.add_argument("--no-orderbook", action="store_true")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--output-dir", "-o", default="backtest_results")
    parser.add_argument("--quiet", "-q", action="store_true")
    
    args = parser.parse_args()
    
    # Validate
    if not STRATEGIES_LOADED:
        print("❌ oracle_strategies.py not found")
        sys.exit(1)
    
    if not args.symbol and not args.symbols and not args.scan:
        parser.error("Must specify --symbol, --symbols, or --scan")
    
    # Parse inputs
    timeframes = [tf.strip() for tf in args.timeframes.split(",")]
    strategy_codes = parse_strategy_filter(args.strategies)
    
    config = StrategyConfig(
        fee=args.fee,
        slippage=args.slippage,
        pos_size=args.pos_size,
        min_band_pct=args.min_band,
        use_order_book=not args.no_orderbook and ORDERBOOK_LOADED
    )
    
    orchestrator = BacktestOrchestrator(
        config=config,
        strategy_codes=strategy_codes,
        capital=args.capital,
        top_n=args.top
    )
    
    exporter = ReportExporter(Path(args.output_dir))
    
    # Execute
    async def run():
        if args.scan:
            mode = ScanMode(args.scan)
            
            if args.periodic:
                await orchestrator.run_periodic(mode, timeframes, args.days, args.periodic)
            else:
                ranking = await orchestrator.run_scan(
                    mode, timeframes, args.days, args.top_n
                )
                
                if not args.quiet:
                    orchestrator._print_ranking_summary(ranking)
                
                exporter.export_ranking_csv(ranking)
                exporter.export_full_json(ranking)
                exporter.export_paper_trading_config(ranking, n=10)
        else:
            # Manual mode
            if args.symbol:
                symbols = [args.symbol]
            else:
                symbols = [s.strip() for s in args.symbols.split(",")]
            
            ranking = await orchestrator.run_manual(symbols, timeframes, args.days)
            
            if not args.quiet:
                orchestrator._print_ranking_summary(ranking)
            
            exporter.export_ranking_csv(ranking)
            exporter.export_full_json(ranking)
    
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user")
    
    print("\n✅ Backtest complete!")


if __name__ == "__main__":
    main()
