#!/usr/bin/env python3
"""
oracle_strategies.py - PRODUCTION STRATEGIES v4.0

OPTIMIZED FOR:
- High-frequency backtesting (80+ pairs)
- Periodic parameter optimization
- Walk-forward testing
- Auto-tuning for paper/live trading
- Memory efficiency for long runs
- Thread-safe operation for parallel backtests

STRATEGIES (23 Total - S1 to S23):
Core (S1-S11):
  S1: Path Clear - Trade when path to opposite band is clear
  S2: Mean Reversion - Fade band extremes with velocity confirmation
  S3: Multi-Touch - Trade strong support/resistance from multiple touches
  S4: Simple Band Entry - Baseline buy at low, sell at high
  S5: Volatility Squeeze - Trade volatility expansion after squeeze
  S6: Tick Clustering - Trade statistical tick/volume clustering
  S7: Fakeout Recovery - Trade failed breakout reversals
  S8: Order Flow Momentum - Trade aggressive order flow momentum
  S9: Wall Fade - Trade expecting large walls to hold
  S10: Spread Normalization - Trade when spread is abnormally tight
  S11: Candle Pattern - Trade candlestick patterns at band edges

Sheep Hunter (S12-S17):
  S12: Stop Hunt Detector - Detect and trade stop hunt reversals
  S13: Iceberg Detector - Detect hidden iceberg orders
  S14: Liquidity Sweep - Detect liquidity sweeps and trade reversal
  S15: Bot Exhaustion - Detect exhausted momentum bots
  S16: Imbalance Divergence - Trade when tick flow diverges from order book
  S17: Momentum Trap - Fade trapped momentum chasers

Advanced Microstructure (S18-S23):
  S18: VWAP Deviation - Trade deviations from VWAP
  S19: Absorption Detector - Detect large volume absorption
  S20: Depth Gradient - Trade based on order book depth gradient
  S21: Queue Imbalance - Trade immediate queue pressure
  S22: Institutional Flow - Detect institutional accumulation/distribution
  S23: Market Making Edge - Capture spread in favorable conditions

Author: Oracle Trading System
Version: 4.0.0
"""

from __future__ import annotations

import hashlib
import logging
import threading
import warnings
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import lru_cache
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore', category=RuntimeWarning)

logger = logging.getLogger(__name__)


# ============================================================
# TYPE ALIASES
# ============================================================

TickTuple = Tuple[int, float]  # (timestamp_ms, price)
TickWithSize = Tuple[int, float, float]  # (timestamp_ms, price, size)
PriceLevel = Tuple[float, float]  # (price, size)


# ============================================================
# CONSTANTS
# ============================================================

class StrategyConstants:
    """Global constants for all strategies - immutable configuration."""
    
    # Confidence bounds
    MIN_CONFIDENCE: float = 0.45
    BASE_CONFIDENCE: float = 0.50
    MAX_CONFIDENCE: float = 0.85
    
    # Default boosts
    OB_CONFIRMATION_BOOST: float = 0.05
    PATTERN_CONFIRMATION_BOOST: float = 0.05
    VOLUME_CONFIRMATION_BOOST: float = 0.03
    CONSENSUS_BOOST_STRONG: float = 0.10
    CONSENSUS_BOOST_MODERATE: float = 0.05
    
    # Default penalties
    CONTRADICTING_PATTERN_PENALTY: float = 0.08
    UNFAVORABLE_REGIME_PENALTY: float = 0.03
    
    # Tick analysis
    MIN_TICKS_FOR_SIGNAL: int = 10
    MIN_TICKS_FOR_VELOCITY: int = 5
    MIN_TICKS_FOR_VOLATILITY: int = 20
    
    # Volatility
    VOLATILITY_SPIKE_MULT: float = 2.0
    VOLATILITY_SQUEEZE_RATIO: float = 0.6
    
    # Zone definitions
    TOUCH_ZONE_PCT: float = 0.05
    CLUSTER_ZONE_PCT: float = 0.15
    
    # Position thresholds (percentage of band)
    EXTREME_LOW: float = 0.15
    LOW: float = 0.25
    MID_LOW: float = 0.40
    MID: float = 0.50
    MID_HIGH: float = 0.60
    HIGH: float = 0.75
    EXTREME_HIGH: float = 0.85
    
    # Performance
    MAX_STRATEGIES_PER_EVALUATION: int = 23
    CACHE_SIZE_LIMIT: int = 1000


# ============================================================
# ENUMS
# ============================================================

class TradeAction(Enum):
    """Trade direction."""
    LONG = "long"
    SHORT = "short"
    NONE = "none"
    
    def __str__(self) -> str:
        return self.value
    
    @property
    def opposite(self) -> 'TradeAction':
        if self == TradeAction.LONG:
            return TradeAction.SHORT
        elif self == TradeAction.SHORT:
            return TradeAction.LONG
        return TradeAction.NONE
    
    @property
    def is_directional(self) -> bool:
        return self in (TradeAction.LONG, TradeAction.SHORT)


class MarketRegime(Enum):
    """Market regime classification."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNKNOWN = "unknown"
    
    @property
    def is_trending(self) -> bool:
        return self in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN)
    
    @property
    def is_volatile(self) -> bool:
        return self == MarketRegime.HIGH_VOLATILITY
    
    @property
    def is_calm(self) -> bool:
        return self in (MarketRegime.RANGING, MarketRegime.LOW_VOLATILITY)


class PositionSizingMethod(Enum):
    """Position sizing methods."""
    FIXED = "fixed"
    KELLY = "kelly"
    VOLATILITY_BASED = "volatility_based"
    ADAPTIVE = "adaptive"


class CandlePattern(Enum):
    """Candlestick pattern types."""
    NONE = "none"
    DOJI = "doji"
    HAMMER = "hammer"
    INVERTED_HAMMER = "inverted_hammer"
    HANGING_MAN = "hanging_man"
    SHOOTING_STAR = "shooting_star"
    BULLISH_ENGULFING = "bullish_engulfing"
    BEARISH_ENGULFING = "bearish_engulfing"
    MORNING_STAR = "morning_star"
    EVENING_STAR = "evening_star"
    THREE_WHITE_SOLDIERS = "three_white_soldiers"
    THREE_BLACK_CROWS = "three_black_crows"
    BULLISH_HARAMI = "bullish_harami"
    BEARISH_HARAMI = "bearish_harami"
    TWEEZER_BOTTOM = "tweezer_bottom"
    TWEEZER_TOP = "tweezer_top"
    PIERCING_LINE = "piercing_line"
    DARK_CLOUD_COVER = "dark_cloud_cover"


# ============================================================
# PARAMETER BOUNDS (FOR OPTIMIZATION)
# ============================================================

@dataclass(frozen=True, slots=True)
class ParamBounds:
    """Immutable bounds for a single optimizable parameter."""
    min_val: float
    max_val: float
    default: float
    step: float = 0.01
    is_integer: bool = False
    
    def clamp(self, value: float) -> float:
        """Clamp value to bounds."""
        if self.is_integer:
            return int(max(self.min_val, min(self.max_val, round(value))))
        return max(self.min_val, min(self.max_val, value))
    
    def random(self, rng: np.random.Generator) -> float:
        """Generate random value within bounds."""
        if self.is_integer:
            return int(rng.integers(int(self.min_val), int(self.max_val) + 1))
        return float(rng.uniform(self.min_val, self.max_val))
    
    def grid(self, n_points: int = 5) -> List[float]:
        """Generate grid of values for optimization."""
        if self.is_integer:
            values = np.linspace(self.min_val, self.max_val, n_points)
            return sorted(set(int(round(v)) for v in values))
        return list(np.linspace(self.min_val, self.max_val, n_points))


# All optimizable parameters with their bounds
PARAM_BOUNDS: Dict[str, ParamBounds] = {
    # Transaction costs
    "fee": ParamBounds(0.0001, 0.005, 0.0004),
    "slippage": ParamBounds(0.0001, 0.005, 0.0005),
    
    # Position sizing
    "pos_size": ParamBounds(0.02, 0.30, 0.10),
    "kelly_fraction": ParamBounds(0.10, 0.50, 0.25),
    
    # Risk management
    "stop_loss_band_multiplier": ParamBounds(0.2, 1.0, 0.5),
    
    # Band requirements
    "min_band_pct": ParamBounds(0.05, 0.50, 0.15),
    
    # S2: Mean Reversion
    "mean_reversion_threshold": ParamBounds(0.10, 0.40, 0.25),
    "velocity_window": ParamBounds(3, 15, 5, is_integer=True),
    "velocity_reversal_boost": ParamBounds(0.01, 0.15, 0.05),
    
    # S3: Multi-Touch
    "min_touches": ParamBounds(1, 5, 2, is_integer=True),
    "max_opposite_touches": ParamBounds(0, 3, 1, is_integer=True),
    
    # S5: Volatility Squeeze
    "volatility_squeeze_ratio": ParamBounds(0.3, 0.8, 0.6),
    
    # S6: Tick Clustering
    "cluster_threshold": ParamBounds(0.25, 0.60, 0.40),
    
    # S7: Fakeout Recovery
    "fakeout_recovery_threshold": ParamBounds(0.002, 0.015, 0.005),
    
    # S8: Order Flow Momentum
    "order_flow_threshold": ParamBounds(0.15, 0.50, 0.30),
    "tick_flow_weight": ParamBounds(0.2, 0.6, 0.4),
    "ob_imbalance_weight": ParamBounds(0.2, 0.6, 0.4),
    
    # S10: Spread Normalization
    "tight_spread_threshold": ParamBounds(0.005, 0.05, 0.02),
    
    # S11: Candle Pattern
    "min_pattern_strength": ParamBounds(0.40, 0.70, 0.55),
    "pattern_edge_threshold": ParamBounds(0.15, 0.35, 0.25),
    
    # S12: Stop Hunt
    "stop_hunt_volatility_mult": ParamBounds(1.5, 4.0, 2.0),
    "stop_hunt_min_spike": ParamBounds(0.001, 0.005, 0.002),
    "stop_hunt_reversal_depth": ParamBounds(0.002, 0.015, 0.005),
    "stop_hunt_max_threshold": ParamBounds(0.02, 0.05, 0.03),
    
    # S13: Iceberg
    "iceberg_stability_threshold": ParamBounds(0.0005, 0.003, 0.001),
    "iceberg_flow_threshold": ParamBounds(0.25, 0.55, 0.40),
    
    # S14: Liquidity Sweep
    "sweep_min_range": ParamBounds(0.08, 0.25, 0.15),
    "sweep_reversal_confirm": ParamBounds(0.02, 0.10, 0.05),
    
    # S15: Bot Exhaustion
    "exhaustion_velocity_decay": ParamBounds(0.3, 0.7, 0.5),
    
    # S16: Imbalance Divergence
    "divergence_threshold": ParamBounds(0.20, 0.50, 0.35),
    
    # S17: Momentum Trap
    "momentum_strong_threshold": ParamBounds(0.008, 0.025, 0.015),
    "momentum_death_threshold": ParamBounds(0.001, 0.005, 0.003),
    
    # S18: VWAP Deviation
    "vwap_deviation_threshold": ParamBounds(0.002, 0.015, 0.005),
    
    # S19: Absorption
    "absorption_volume_ratio": ParamBounds(1.5, 4.0, 2.5),
    "absorption_price_stability": ParamBounds(0.0003, 0.002, 0.0008),
    
    # S20: Depth Gradient
    "depth_gradient_threshold": ParamBounds(0.15, 0.50, 0.30),
    
    # S21: Queue Imbalance
    "queue_imbalance_threshold": ParamBounds(0.20, 0.60, 0.40),
    
    # S22: Institutional Flow
    "institutional_size_mult": ParamBounds(2.0, 8.0, 4.0),
    "institutional_cluster_count": ParamBounds(2, 8, 4, is_integer=True),
    
    # S23: Market Making Edge
    "mm_min_spread_bps": ParamBounds(3.0, 15.0, 6.0),
    "mm_imbalance_threshold": ParamBounds(0.05, 0.25, 0.10),
    
    # Order book settings
    "min_imbalance_for_entry": ParamBounds(0.05, 0.30, 0.10),
    "imbalance_trend_threshold": ParamBounds(0.005, 0.03, 0.01),
    "max_spread_pct": ParamBounds(0.05, 0.30, 0.15),
    "depth_levels": ParamBounds(5, 20, 10, is_integer=True),
    
    # Confidence settings
    "min_confidence": ParamBounds(0.45, 0.60, 0.50),
    "high_confidence_threshold": ParamBounds(0.60, 0.75, 0.65),
    
    # Consensus
    "min_strategies_for_consensus": ParamBounds(2, 5, 2, is_integer=True),
    "consensus_ratio_threshold": ParamBounds(0.55, 0.85, 0.70),
}


def get_param_bounds(name: str) -> ParamBounds:
    """Get bounds for a parameter, with fallback to default range."""
    return PARAM_BOUNDS.get(name, ParamBounds(0.0, 1.0, 0.5))


# ============================================================
# STRATEGY-SPECIFIC PARAMETERS
# ============================================================

@dataclass
class StrategyParams:
    """
    Per-strategy parameter overrides for optimization.
    
    Each strategy can have its own tuned parameters that override
    the global StrategyConfig values.
    """
    enabled: bool = True
    weight: float = 1.0  # For ensemble weighting
    params: Dict[str, float] = field(default_factory=dict)
    
    def get(self, key: str, default: Optional[float] = None) -> float:
        """Get parameter value with fallback to default."""
        if key in self.params:
            return self.params[key]
        if default is not None:
            return default
        bounds = get_param_bounds(key)
        return bounds.default
    
    def set(self, key: str, value: float) -> None:
        """Set parameter with bounds checking."""
        bounds = get_param_bounds(key)
        self.params[key] = bounds.clamp(value)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "weight": self.weight,
            "params": self.params.copy(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StrategyParams':
        return cls(
            enabled=data.get("enabled", True),
            weight=data.get("weight", 1.0),
            params=data.get("params", {}).copy(),
        )


# ============================================================
# MAIN CONFIGURATION
# ============================================================

@dataclass
class StrategyConfig:
    """
    Master strategy configuration with comprehensive validation.
    
    Supports:
    - Per-strategy parameter overrides
    - Parameter bounds for optimization
    - Regime-specific parameter sets
    - Serialization for persistence
    """
    
    # -------------------- Transaction Costs --------------------
    fee: float = 0.0004
    slippage: float = 0.0005
    
    # -------------------- Position Sizing --------------------
    pos_size: float = 0.10
    max_position_size: float = 0.25
    min_position_size: float = 0.02
    position_sizing_method: PositionSizingMethod = PositionSizingMethod.FIXED
    
    # -------------------- Kelly Parameters --------------------
    kelly_fraction: float = 0.25
    default_win_rate: float = 0.52
    default_win_loss_ratio: float = 1.5
    
    # -------------------- Volatility Sizing --------------------
    target_volatility: float = 0.02
    vol_lookback: int = 20
    
    # -------------------- Risk Management --------------------
    use_stop_loss: bool = True
    stop_loss_band_multiplier: float = 0.5
    max_drawdown_pct: float = 0.10
    
    # -------------------- Edge Requirements --------------------
    min_edge_multiplier: float = 2.0
    
    # -------------------- Band Parameters --------------------
    min_band_pct: float = 0.15
    
    # -------------------- S2: Mean Reversion --------------------
    mean_reversion_threshold: float = 0.25
    velocity_window: int = 5
    require_velocity_reversal: bool = False
    velocity_reversal_boost: float = 0.05
    
    # -------------------- S3: Multi-Touch --------------------
    min_touches: int = 2
    max_opposite_touches: int = 1
    
    # -------------------- S5: Volatility Squeeze --------------------
    volatility_squeeze_ratio: float = 0.6
    
    # -------------------- S6: Tick Clustering --------------------
    cluster_threshold: float = 0.40
    
    # -------------------- S7: Fakeout Recovery --------------------
    fakeout_recovery_threshold: float = 0.005
    
    # -------------------- S8: Order Flow Momentum --------------------
    order_flow_threshold: float = 0.30
    tick_flow_weight: float = 0.4
    ob_imbalance_weight: float = 0.4
    ob_trend_weight: float = 0.2
    
    # -------------------- S10: Spread Normalization --------------------
    tight_spread_threshold: float = 0.02
    
    # -------------------- S11: Candle Pattern --------------------
    min_pattern_strength: float = 0.55
    pattern_edge_threshold: float = 0.25
    
    # -------------------- S12: Stop Hunt --------------------
    stop_hunt_volatility_mult: float = 2.0
    stop_hunt_min_spike: float = 0.002
    stop_hunt_reversal_depth: float = 0.005
    stop_hunt_max_threshold: float = 0.03
    
    # -------------------- S13: Iceberg --------------------
    iceberg_stability_threshold: float = 0.001
    iceberg_flow_threshold: float = 0.40
    
    # -------------------- S14: Liquidity Sweep --------------------
    sweep_min_range: float = 0.15
    sweep_reversal_confirm: float = 0.05
    
    # -------------------- S15: Bot Exhaustion --------------------
    exhaustion_velocity_decay: float = 0.5
    
    # -------------------- S16: Imbalance Divergence --------------------
    divergence_threshold: float = 0.35
    
    # -------------------- S17: Momentum Trap --------------------
    momentum_strong_threshold: float = 0.015
    momentum_death_threshold: float = 0.003
    
    # -------------------- S18-S23: Advanced Strategies --------------------
    vwap_deviation_threshold: float = 0.005
    absorption_volume_ratio: float = 2.5
    absorption_price_stability: float = 0.0008
    depth_gradient_threshold: float = 0.30
    queue_imbalance_threshold: float = 0.40
    institutional_size_mult: float = 4.0
    institutional_cluster_count: int = 4
    mm_min_spread_bps: float = 6.0
    mm_imbalance_threshold: float = 0.10
    
    # -------------------- Order Book Settings --------------------
    use_order_book: bool = True
    require_order_book: bool = False
    max_spread_pct: float = 0.15
    min_imbalance_for_entry: float = 0.1
    use_depth_imbalance: bool = True
    depth_levels: int = 10
    imbalance_trend_threshold: float = 0.01
    
    # -------------------- Volume Settings --------------------
    require_volume_confirmation: bool = False
    min_volume_ratio: float = 1.0
    volume_lookback: int = 20
    use_volume_weighting: bool = True
    
    # -------------------- Candle Pattern Settings --------------------
    use_candle_patterns: bool = True
    candle_pattern_confidence_boost: float = 0.05
    candle_pattern_contradiction_penalty: float = 0.08
    doji_body_pct: float = 0.1
    
    # -------------------- Regime Settings --------------------
    use_regime_filter: bool = True
    regime_boost: float = 0.05
    regime_penalty: float = 0.03
    skip_unfavorable_regimes: bool = False  # If True, skip evaluation entirely
    
    # -------------------- Confidence Settings --------------------
    min_confidence: float = 0.50
    high_confidence_threshold: float = 0.65
    max_confidence: float = 0.85
    
    # -------------------- Consensus Settings --------------------
    use_consensus_boost: bool = True
    min_strategies_for_consensus: int = 2
    consensus_ratio_threshold: float = 0.70
    
    # -------------------- Per-Strategy Overrides --------------------
    strategy_params: Dict[str, StrategyParams] = field(default_factory=dict)
    
    # -------------------- Regime-Specific Parameters --------------------
    regime_params: Dict[str, Dict[str, float]] = field(default_factory=dict)
    
    def __post_init__(self) -> None:
        """Validate all configuration parameters."""
        errors: List[str] = []
        
        # Transaction cost validation
        if not (0 <= self.fee < 0.01):
            errors.append(f"fee must be in [0, 0.01), got {self.fee}")
        
        if not (0 <= self.slippage < 0.01):
            errors.append(f"slippage must be in [0, 0.01), got {self.slippage}")
        
        # Position size chain validation
        if not (0 < self.min_position_size <= self.pos_size <= self.max_position_size <= 1):
            errors.append(
                f"Invalid position size chain: "
                f"min={self.min_position_size}, pos={self.pos_size}, max={self.max_position_size}"
            )
        
        # Confidence threshold chain validation
        if not (0 < self.min_confidence < self.high_confidence_threshold <= self.max_confidence <= 1):
            errors.append(
                f"Invalid confidence chain: "
                f"min={self.min_confidence}, high={self.high_confidence_threshold}, max={self.max_confidence}"
            )
        
        if errors:
            raise ValueError("Configuration errors:\n  - " + "\n  - ".join(errors))
    
    @property
    def total_cost(self) -> float:
        """Total round-trip transaction cost."""
        return (self.fee + self.slippage) * 2
    
    @property
    def min_edge(self) -> float:
        """Minimum required edge to enter trade."""
        return self.total_cost * self.min_edge_multiplier
    
    def get_strategy_param(
        self,
        strategy_code: str,
        param_name: str,
        default: Optional[float] = None,
    ) -> float:
        """
        Get parameter for specific strategy with fallback chain:
        1. Strategy-specific override
        2. Global config attribute
        3. Provided default
        4. Parameter bounds default
        """
        # Check strategy-specific override
        if strategy_code in self.strategy_params:
            sp = self.strategy_params[strategy_code]
            if param_name in sp.params:
                return sp.params[param_name]
        
        # Check global config attribute
        if hasattr(self, param_name):
            return getattr(self, param_name)
        
        # Use provided default
        if default is not None:
            return default
        
        # Fall back to parameter bounds default
        return get_param_bounds(param_name).default
    
    def get_regime_param(
        self,
        regime: MarketRegime,
        param_name: str,
    ) -> float:
        """Get regime-specific parameter with fallback to global."""
        regime_key = regime.value
        if regime_key in self.regime_params:
            if param_name in self.regime_params[regime_key]:
                return self.regime_params[regime_key][param_name]
        return getattr(self, param_name, get_param_bounds(param_name).default)
    
    def is_strategy_enabled(self, code: str) -> bool:
        """Check if strategy is enabled."""
        if code in self.strategy_params:
            return self.strategy_params[code].enabled
        return True
    
    def get_strategy_weight(self, code: str) -> float:
        """Get strategy weight for ensemble."""
        if code in self.strategy_params:
            return self.strategy_params[code].weight
        return 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for persistence."""
        result = {}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if isinstance(value, Enum):
                result[field_name] = value.value
            elif isinstance(value, dict) and field_name == "strategy_params":
                result[field_name] = {k: v.to_dict() for k, v in value.items()}
            else:
                result[field_name] = value
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StrategyConfig':
        """Create from dictionary."""
        processed = data.copy()
        
        # Handle enum conversion
        if 'position_sizing_method' in processed:
            val = processed['position_sizing_method']
            if isinstance(val, str):
                processed['position_sizing_method'] = PositionSizingMethod(val)
        
        # Handle strategy params
        if 'strategy_params' in processed:
            processed['strategy_params'] = {
                k: StrategyParams.from_dict(v)
                for k, v in processed['strategy_params'].items()
            }
        
        # Filter to valid fields only
        valid_fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in processed.items() if k in valid_fields}
        
        return cls(**filtered)
    
    def copy(self, **overrides) -> 'StrategyConfig':
        """Create copy with optional overrides."""
        data = self.to_dict()
        data.update(overrides)
        return StrategyConfig.from_dict(data)
    
    @classmethod
    def for_optimization(cls) -> Dict[str, ParamBounds]:
        """Get all optimizable parameters with bounds."""
        return PARAM_BOUNDS.copy()
    
    def get_optimization_vector(self) -> np.ndarray:
        """Get current config as optimization vector."""
        values = []
        for name in sorted(PARAM_BOUNDS.keys()):
            if hasattr(self, name):
                values.append(getattr(self, name))
            else:
                values.append(PARAM_BOUNDS[name].default)
        return np.array(values, dtype=np.float64)
    
    @classmethod
    def from_optimization_vector(cls, vector: np.ndarray) -> 'StrategyConfig':
        """Create config from optimization vector."""
        params = {}
        for i, name in enumerate(sorted(PARAM_BOUNDS.keys())):
            if i < len(vector):
                bounds = PARAM_BOUNDS[name]
                params[name] = bounds.clamp(vector[i])
        return cls(**{k: v for k, v in params.items() if k in cls.__dataclass_fields__})


# ============================================================
# DETECTED PATTERN
# ============================================================

@dataclass(frozen=True, slots=True)
class DetectedPattern:
    """Immutable detected candle pattern with slots for memory efficiency."""
    
    pattern: CandlePattern
    direction: str  # 'bullish', 'bearish', 'neutral'
    strength: float  # 0.0 to 1.0
    candles_used: int
    
    @property
    def is_bullish(self) -> bool:
        return self.direction == 'bullish'
    
    @property
    def is_bearish(self) -> bool:
        return self.direction == 'bearish'
    
    @property
    def is_neutral(self) -> bool:
        return self.direction == 'neutral'
    
    def supports_direction(self, action: TradeAction) -> bool:
        """Check if pattern supports trade direction."""
        if action == TradeAction.LONG:
            return self.is_bullish
        elif action == TradeAction.SHORT:
            return self.is_bearish
        return False
    
    def contradicts_direction(self, action: TradeAction) -> bool:
        """Check if pattern contradicts trade direction."""
        if action == TradeAction.LONG:
            return self.is_bearish
        elif action == TradeAction.SHORT:
            return self.is_bullish
        return False


# ============================================================
# CANDLE PATTERN DETECTOR (Optimized with Content-Based Cache)
# ============================================================

class CandlePatternDetector:
    """
    Detect candlestick patterns with content-based caching.
    
    Optimizations:
    - Content-based cache key (safe from id() reuse)
    - Lazy computation (only when needed)
    - Early exit for insufficient data
    - Vectorized calculations where possible
    """
    
    __slots__ = (
        'doji_body_pct',
        'engulfing_min_ratio',
        '_cache_key',
        '_cache_result',
    )
    
    def __init__(
        self,
        doji_body_pct: float = 0.1,
        engulfing_min_ratio: float = 1.5,
    ) -> None:
        self.doji_body_pct = doji_body_pct
        self.engulfing_min_ratio = engulfing_min_ratio
        self._cache_key: Optional[int] = None
        self._cache_result: List[DetectedPattern] = []
    
    def _compute_cache_key(self, candle_data: pd.DataFrame) -> int:
        """Create stable cache key from actual data content."""
        if candle_data is None or len(candle_data) == 0:
            return 0
        
        # Use first, last, and middle candle data + length
        first = candle_data.iloc[0]
        last = candle_data.iloc[-1]
        
        try:
            key_tuple = (
                len(candle_data),
                round(float(first['open']), 6),
                round(float(first['close']), 6),
                round(float(last['open']), 6),
                round(float(last['close']), 6),
                round(float(last['high']), 6),
                round(float(last['low']), 6),
            )
            return hash(key_tuple)
        except (KeyError, TypeError):
            return 0
    
    def detect_all(self, candle_data: pd.DataFrame) -> List[DetectedPattern]:
        """Detect all patterns with caching."""
        if candle_data is None or len(candle_data) < 1:
            return []
        
        # Check cache using content-based key
        cache_key = self._compute_cache_key(candle_data)
        if cache_key != 0 and self._cache_key == cache_key:
            return self._cache_result
        
        # Validate columns
        required = {'open', 'high', 'low', 'close'}
        if not required.issubset(candle_data.columns):
            return []
        
        patterns: List[DetectedPattern] = []
        
        # Single candle patterns (most common)
        patterns.extend(self._detect_single_patterns(candle_data))
        
        # Two candle patterns
        if len(candle_data) >= 2:
            patterns.extend(self._detect_two_candle_patterns(candle_data))
        
        # Three candle patterns
        if len(candle_data) >= 3:
            patterns.extend(self._detect_three_candle_patterns(candle_data))
        
        # Sort by strength (strongest first)
        patterns.sort(key=lambda x: x.strength, reverse=True)
        
        # Update cache
        self._cache_key = cache_key
        self._cache_result = patterns
        
        return patterns
    
    def get_strongest_pattern(
        self,
        candle_data: pd.DataFrame,
    ) -> Optional[DetectedPattern]:
        """Get the strongest detected pattern."""
        patterns = self.detect_all(candle_data)
        return patterns[0] if patterns else None
    
    def get_strongest_bullish(
        self,
        candle_data: pd.DataFrame,
    ) -> Optional[DetectedPattern]:
        """Get the strongest bullish pattern."""
        patterns = self.detect_all(candle_data)
        bullish = [p for p in patterns if p.is_bullish]
        return bullish[0] if bullish else None
    
    def get_strongest_bearish(
        self,
        candle_data: pd.DataFrame,
    ) -> Optional[DetectedPattern]:
        """Get the strongest bearish pattern."""
        patterns = self.detect_all(candle_data)
        bearish = [p for p in patterns if p.is_bearish]
        return bearish[0] if bearish else None
    
    @staticmethod
    def _get_candle_metrics(row: pd.Series) -> Dict[str, float]:
        """Calculate candle metrics efficiently."""
        try:
            o = float(row['open'])
            h = float(row['high'])
            l = float(row['low'])
            c = float(row['close'])
        except (KeyError, TypeError, ValueError):
            return {}
        
        body = abs(c - o)
        full_range = h - l if h > l else 1e-10
        
        return {
            'open': o,
            'high': h,
            'low': l,
            'close': c,
            'body': body,
            'range': full_range,
            'body_pct': body / full_range,
            'upper_shadow': h - max(o, c),
            'lower_shadow': min(o, c) - l,
            'upper_shadow_pct': (h - max(o, c)) / full_range,
            'lower_shadow_pct': (min(o, c) - l) / full_range,
            'is_bullish': c > o,
            'is_bearish': c < o,
        }
    
    def _detect_single_patterns(
        self,
        candle_data: pd.DataFrame,
    ) -> List[DetectedPattern]:
        """Detect single-candle patterns from last candle."""
        patterns: List[DetectedPattern] = []
        
        last = candle_data.iloc[-1]
        m = self._get_candle_metrics(last)
        
        if not m:
            return patterns
        
        # Doji - small body, indecision
        if m['body_pct'] < self.doji_body_pct:
            body_center = (m['open'] + m['close']) / 2
            range_center = (m['high'] + m['low']) / 2
            centering = 1 - abs(body_center - range_center) / m['range']
            patterns.append(DetectedPattern(
                pattern=CandlePattern.DOJI,
                direction='neutral',
                strength=0.5 * centering,
                candles_used=1,
            ))
        
        # Hammer - bullish reversal
        if (m['body_pct'] < 0.3 and
            m['lower_shadow_pct'] >= 0.5 and
            m['upper_shadow_pct'] < 0.15):
            strength = min(0.85, 0.55 + (m['lower_shadow_pct'] - 0.5) * 0.5)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.HAMMER,
                direction='bullish',
                strength=strength,
                candles_used=1,
            ))
        
        # Shooting Star - bearish reversal (must be bearish candle)
        if (m['body_pct'] < 0.3 and
            m['upper_shadow_pct'] >= 0.5 and
            m['lower_shadow_pct'] < 0.15 and
            m['is_bearish']):
            strength = min(0.80, 0.55 + (m['upper_shadow_pct'] - 0.5) * 0.4)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.SHOOTING_STAR,
                direction='bearish',
                strength=strength,
                candles_used=1,
            ))
        
        # Inverted Hammer - bullish (must be bullish candle)
        if (m['body_pct'] < 0.3 and
            m['upper_shadow_pct'] >= 0.5 and
            m['lower_shadow_pct'] < 0.15 and
            m['is_bullish']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.INVERTED_HAMMER,
                direction='bullish',
                strength=0.55,
                candles_used=1,
            ))
        
        # Hanging Man - bearish (must be bearish candle)
        if (m['body_pct'] < 0.3 and
            m['lower_shadow_pct'] >= 0.5 and
            m['upper_shadow_pct'] < 0.15 and
            m['is_bearish']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.HANGING_MAN,
                direction='bearish',
                strength=0.55,
                candles_used=1,
            ))
        
        return patterns
    
    def _detect_two_candle_patterns(
        self,
        candle_data: pd.DataFrame,
    ) -> List[DetectedPattern]:
        """Detect two-candle patterns."""
        patterns: List[DetectedPattern] = []
        
        m1 = self._get_candle_metrics(candle_data.iloc[-2])
        m2 = self._get_candle_metrics(candle_data.iloc[-1])
        
        if not m1 or not m2:
            return patterns
        
        # Early exit if both have tiny bodies
        if m1['body_pct'] < 0.05 and m2['body_pct'] < 0.05:
            return patterns
        
        # Bullish Engulfing
        if (m1['is_bearish'] and m2['is_bullish'] and
            m2['open'] <= m1['close'] and
            m2['close'] >= m1['open'] and
            m2['body'] > m1['body'] * self.engulfing_min_ratio):
            
            size_ratio = m2['body'] / max(m1['body'], 1e-10)
            strength = min(0.85, 0.65 + (size_ratio - 1) * 0.08)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.BULLISH_ENGULFING,
                direction='bullish',
                strength=strength,
                candles_used=2,
            ))
        
        # Bearish Engulfing
        if (m1['is_bullish'] and m2['is_bearish'] and
            m2['open'] >= m1['close'] and
            m2['close'] <= m1['open'] and
            m2['body'] > m1['body'] * self.engulfing_min_ratio):
            
            size_ratio = m2['body'] / max(m1['body'], 1e-10)
            strength = min(0.85, 0.65 + (size_ratio - 1) * 0.08)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.BEARISH_ENGULFING,
                direction='bearish',
                strength=strength,
                candles_used=2,
            ))
        
        # Bullish Harami
        if (m1['is_bearish'] and m2['is_bullish'] and
            m1['body'] > m2['body'] * 2 and
            m2['open'] >= m1['close'] and
            m2['close'] <= m1['open']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.BULLISH_HARAMI,
                direction='bullish',
                strength=0.55,
                candles_used=2,
            ))
        
        # Bearish Harami
        if (m1['is_bullish'] and m2['is_bearish'] and
            m1['body'] > m2['body'] * 2 and
            m2['open'] <= m1['close'] and
            m2['close'] >= m1['open']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.BEARISH_HARAMI,
                direction='bearish',
                strength=0.55,
                candles_used=2,
            ))
        
        # Tweezer patterns (matching highs/lows)
        if m1['range'] > 0:
            low_diff_pct = abs(m1['low'] - m2['low']) / m1['range']
            high_diff_pct = abs(m1['high'] - m2['high']) / m1['range']
            
            # Tweezer Bottom
            if low_diff_pct < 0.03 and m1['is_bearish'] and m2['is_bullish']:
                patterns.append(DetectedPattern(
                    pattern=CandlePattern.TWEEZER_BOTTOM,
                    direction='bullish',
                    strength=0.60,
                    candles_used=2,
                ))
            
            # Tweezer Top
            if high_diff_pct < 0.03 and m1['is_bullish'] and m2['is_bearish']:
                patterns.append(DetectedPattern(
                    pattern=CandlePattern.TWEEZER_TOP,
                    direction='bearish',
                    strength=0.60,
                    candles_used=2,
                ))
        
        # Piercing Line
        if (m1['is_bearish'] and m2['is_bullish'] and
            m2['open'] < m1['low'] and
            m2['close'] > (m1['open'] + m1['close']) / 2 and
            m2['close'] < m1['open'] and
            m1['body'] > 0):
            
            penetration = (m2['close'] - m1['close']) / m1['body']
            strength = min(0.70, 0.55 + penetration * 0.2)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.PIERCING_LINE,
                direction='bullish',
                strength=strength,
                candles_used=2,
            ))
        
        # Dark Cloud Cover
        if (m1['is_bullish'] and m2['is_bearish'] and
            m2['open'] > m1['high'] and
            m2['close'] < (m1['open'] + m1['close']) / 2 and
            m2['close'] > m1['open'] and
            m1['body'] > 0):
            
            penetration = (m1['close'] - m2['close']) / m1['body']
            strength = min(0.70, 0.55 + penetration * 0.2)
            patterns.append(DetectedPattern(
                pattern=CandlePattern.DARK_CLOUD_COVER,
                direction='bearish',
                strength=strength,
                candles_used=2,
            ))
        
        return patterns
    
    def _detect_three_candle_patterns(
        self,
        candle_data: pd.DataFrame,
    ) -> List[DetectedPattern]:
        """Detect three-candle patterns."""
        patterns: List[DetectedPattern] = []
        
        m1 = self._get_candle_metrics(candle_data.iloc[-3])
        m2 = self._get_candle_metrics(candle_data.iloc[-2])
        m3 = self._get_candle_metrics(candle_data.iloc[-1])
        
        if not m1 or not m2 or not m3:
            return patterns
        
        # Morning Star - bullish reversal
        if (m1['is_bearish'] and m1['body_pct'] > 0.4 and
            m2['body_pct'] < 0.25 and
            m3['is_bullish'] and m3['body_pct'] > 0.4 and
            m3['close'] > (m1['open'] + m1['close']) / 2):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.MORNING_STAR,
                direction='bullish',
                strength=0.75,
                candles_used=3,
            ))
        
        # Evening Star - bearish reversal
        if (m1['is_bullish'] and m1['body_pct'] > 0.4 and
            m2['body_pct'] < 0.25 and
            m3['is_bearish'] and m3['body_pct'] > 0.4 and
            m3['close'] < (m1['open'] + m1['close']) / 2):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.EVENING_STAR,
                direction='bearish',
                strength=0.75,
                candles_used=3,
            ))
        
        # Three White Soldiers - strong bullish
        if (m1['is_bullish'] and m2['is_bullish'] and m3['is_bullish'] and
            m1['body_pct'] > 0.4 and m2['body_pct'] > 0.4 and m3['body_pct'] > 0.4 and
            m2['close'] > m1['close'] and m3['close'] > m2['close'] and
            m2['open'] > m1['open'] and m3['open'] > m2['open'] and
            m2['open'] < m1['close'] and m3['open'] < m2['close']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.THREE_WHITE_SOLDIERS,
                direction='bullish',
                strength=0.80,
                candles_used=3,
            ))
        
        # Three Black Crows - strong bearish
        if (m1['is_bearish'] and m2['is_bearish'] and m3['is_bearish'] and
            m1['body_pct'] > 0.4 and m2['body_pct'] > 0.4 and m3['body_pct'] > 0.4 and
            m2['close'] < m1['close'] and m3['close'] < m2['close'] and
            m2['open'] < m1['open'] and m3['open'] < m2['open'] and
            m2['open'] > m1['close'] and m3['open'] > m2['close']):
            patterns.append(DetectedPattern(
                pattern=CandlePattern.THREE_BLACK_CROWS,
                direction='bearish',
                strength=0.80,
                candles_used=3,
            ))
        
        return patterns
    
    def clear_cache(self) -> None:
        """Clear pattern cache."""
        self._cache_key = None
        self._cache_result = []


# ============================================================
# MARKET REGIME DETECTOR
# ============================================================

class MarketRegimeDetector:
    """
    Detect current market regime from volatility and velocity.
    
    Thread-safe and stateless for parallel backtesting.
    """
    
    __slots__ = ('high_vol_threshold', 'low_vol_threshold', 'trend_threshold')
    
    def __init__(
        self,
        high_vol_threshold: float = 0.02,
        low_vol_threshold: float = 0.005,
        trend_threshold: float = 0.002,
    ) -> None:
        self.high_vol_threshold = high_vol_threshold
        self.low_vol_threshold = low_vol_threshold
        self.trend_threshold = trend_threshold
    
    def detect(self, volatility: float, velocity: float) -> MarketRegime:
        """Detect regime from volatility and velocity metrics."""
        # High volatility takes precedence
        if volatility > self.high_vol_threshold:
            return MarketRegime.HIGH_VOLATILITY
        
        # Low volatility
        if volatility < self.low_vol_threshold:
            return MarketRegime.LOW_VOLATILITY
        
        # Trending
        if velocity > self.trend_threshold:
            return MarketRegime.TRENDING_UP
        elif velocity < -self.trend_threshold:
            return MarketRegime.TRENDING_DOWN
        
        # Default to ranging
        return MarketRegime.RANGING
    
    def detect_from_prices(
        self,
        prices: np.ndarray,
        vol_window: int = 20,
        trend_window: int = 10,
    ) -> MarketRegime:
        """Detect regime from price array."""
        if len(prices) < max(vol_window, trend_window):
            return MarketRegime.UNKNOWN
        
        # Calculate volatility from returns
        price_slice = prices[-vol_window:]
        with np.errstate(divide='ignore', invalid='ignore'):
            returns = np.diff(price_slice) / price_slice[:-1]
            returns = returns[np.isfinite(returns)]
        
        if len(returns) < 2:
            return MarketRegime.UNKNOWN
        
        volatility = float(np.std(returns))
        
        # Calculate velocity (normalized price change)
        if len(prices) >= trend_window and prices[-1] > 0:
            velocity = (prices[-1] - prices[-trend_window]) / (prices[-1] * trend_window)
        else:
            velocity = 0.0
        
        return self.detect(volatility, velocity)

# ============================================================
# PRECOMPUTED METRICS
# ============================================================

@dataclass
class PrecomputedMetrics:
    """
    Pre-computed metrics for strategy evaluation.
    
    All expensive calculations are done ONCE per candle and stored here.
    Strategies read from this instead of recalculating.
    
    Extended with comprehensive order book metrics for accurate signal generation.
    """
    
    # -------------------- Price Metrics --------------------
    last_price: float = 0.0
    first_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    price_range: float = 0.0
    
    # -------------------- Position in Band --------------------
    position_in_band: float = 0.5
    distance_to_low: float = 0.5
    distance_to_high: float = 0.5
    
    # -------------------- Velocity (Multiple Windows) --------------------
    velocity_5: float = 0.0
    velocity_10: float = 0.0
    velocity_20: float = 0.0
    velocity_30: float = 0.0
    
    # -------------------- Velocity Reversal --------------------
    velocity_reversed: bool = False
    reversal_direction: str = "none"  # "up", "down", "none"
    
    # -------------------- Volatility (Multiple Windows) --------------------
    volatility_10: float = 0.0
    volatility_20: float = 0.0
    volatility_50: float = 0.0
    
    # -------------------- Order Book Core Metrics --------------------
    ob_available: bool = False
    ob_stale: bool = False  # True if order book data is too old
    ob_imbalance: float = 0.0  # Top-of-book imbalance
    ob_depth_imbalance: float = 0.0  # Multi-level depth imbalance
    ob_spread_pct: float = 0.0  # Spread as percentage
    ob_spread_bps: float = 0.0  # Spread in basis points
    ob_imbalance_trend: float = 0.0  # Trend of imbalance over time
    ob_is_bullish: bool = False  # Order book favors longs
    ob_is_bearish: bool = False  # Order book favors shorts
    
    # -------------------- Order Book Advanced Metrics --------------------
    ob_microprice: float = 0.0  # Size-weighted fair price
    ob_weighted_mid: float = 0.0  # Volume-weighted mid price
    ob_mid_price: float = 0.0  # Simple mid price
    ob_best_bid: float = 0.0
    ob_best_ask: float = 0.0
    ob_best_bid_size: float = 0.0
    ob_best_ask_size: float = 0.0
    
    # -------------------- Order Book Walls --------------------
    has_bid_wall: bool = False
    has_ask_wall: bool = False
    bid_wall_price: float = 0.0
    ask_wall_price: float = 0.0
    bid_wall_size: float = 0.0
    ask_wall_size: float = 0.0
    
    # -------------------- Depth Gradient (S20) --------------------
    bid_depth_gradient: float = 0.0  # Negative = support building
    ask_depth_gradient: float = 0.0  # Positive = resistance building
    
    # -------------------- Order Book Volume --------------------
    ob_bid_volume_5: float = 0.0
    ob_ask_volume_5: float = 0.0
    ob_bid_volume_10: float = 0.0
    ob_ask_volume_10: float = 0.0
    ob_total_bid_value: float = 0.0
    ob_total_ask_value: float = 0.0
    
    # -------------------- Enhanced OB Metrics (v4.1) --------------------
    ob_value_imbalance: float = 0.0
    ob_liquidity_ratio: float = 1.0
    nearest_bid_wall_distance_pct: float = 999.0
    nearest_ask_wall_distance_pct: float = 999.0
    
    # -------------------- Tick Flow Metrics --------------------
    tick_flow: float = 0.0  # Overall buy/sell imbalance
    tick_flow_5: float = 0.0  # Short-term flow
    tick_flow_10: float = 0.0  # Medium-term flow
    tick_flow_20: float = 0.0  # Longer-term flow
    
    # -------------------- Volume Metrics --------------------
    total_volume: float = 0.0
    avg_tick_size: float = 1.0
    volume_ratio: float = 1.0  # Current vs historical average
    
    # -------------------- VWAP Metrics (S18) --------------------
    vwap: float = 0.0
    vwap_deviation: float = 0.0  # (last_price - vwap) / vwap
    
    # -------------------- Large Trade Detection (S22) --------------------
    large_trade_count: int = 0
    large_trade_buy_count: int = 0
    large_trade_sell_count: int = 0
    large_trade_buy_ratio: float = 0.5
    large_trade_total_volume: float = 0.0
    
    # -------------------- Touch Analysis --------------------
    touched_low: bool = False
    touched_high: bool = False
    low_touches: int = 0
    high_touches: int = 0
    
    # -------------------- Clustering Analysis --------------------
    low_cluster_ratio: float = 0.0
    high_cluster_ratio: float = 0.0
    
    # -------------------- Candle Patterns (Lazy) --------------------
    patterns: List[DetectedPattern] = field(default_factory=list)
    strongest_bullish_pattern: Optional[DetectedPattern] = None
    strongest_bearish_pattern: Optional[DetectedPattern] = None
    patterns_computed: bool = False
    
    # -------------------- Market Regime --------------------
    regime: MarketRegime = MarketRegime.UNKNOWN
    
    # -------------------- Metadata --------------------
    tick_count: int = 0
    timestamp: int = 0
    
    # -------------------- Convenience Properties --------------------
    
    @property
    def touched_low_only(self) -> bool:
        """Price touched low band but never high."""
        return self.touched_low and not self.touched_high
    
    @property
    def touched_high_only(self) -> bool:
        """Price touched high band but never low."""
        return self.touched_high and not self.touched_low
    
    @property
    def touched_both(self) -> bool:
        """Price touched both bands."""
        return self.touched_low and self.touched_high
    
    @property
    def touched_neither(self) -> bool:
        """Price touched neither band."""
        return not self.touched_low and not self.touched_high
    
    @property
    def is_at_extreme_low(self) -> bool:
        """Price is at extreme low of band."""
        return self.position_in_band < StrategyConstants.EXTREME_LOW
    
    @property
    def is_at_extreme_high(self) -> bool:
        """Price is at extreme high of band."""
        return self.position_in_band > StrategyConstants.EXTREME_HIGH
    
    @property
    def is_near_low(self) -> bool:
        """Price is near low of band."""
        return self.position_in_band < StrategyConstants.LOW
    
    @property
    def is_near_high(self) -> bool:
        """Price is near high of band."""
        return self.position_in_band > StrategyConstants.HIGH
    
    @property
    def is_mid_range(self) -> bool:
        """Price is in middle of band."""
        return StrategyConstants.MID_LOW < self.position_in_band < StrategyConstants.MID_HIGH
    
    @property
    def ob_is_valid(self) -> bool:
        """Order book is available and not stale."""
        return self.ob_available and not self.ob_stale
    
    @property
    def has_strong_bid_support(self) -> bool:
        """Strong buying pressure in order book."""
        return (
            self.ob_available and
            self.ob_imbalance > 0.2 and
            self.ob_depth_imbalance > 0.15
        )
    
    @property
    def has_strong_ask_resistance(self) -> bool:
        """Strong selling pressure in order book."""
        return (
            self.ob_available and
            self.ob_imbalance < -0.2 and
            self.ob_depth_imbalance < -0.15
        )
    
    def has_bullish_pattern(self, min_strength: float = 0.5) -> bool:
        """Check if bullish pattern exists above threshold."""
        if self.strongest_bullish_pattern is None:
            return False
        return self.strongest_bullish_pattern.strength >= min_strength
    
    def has_bearish_pattern(self, min_strength: float = 0.5) -> bool:
        """Check if bearish pattern exists above threshold."""
        if self.strongest_bearish_pattern is None:
            return False
        return self.strongest_bearish_pattern.strength >= min_strength
    
    @property
    def wall_support_nearby(self) -> bool:
        """Bid wall within 0.5% of current price."""
        return self.has_bid_wall and self.nearest_bid_wall_distance_pct < 0.5
    
    @property
    def wall_resistance_nearby(self) -> bool:
        """Ask wall within 0.5% of current price."""
        return self.has_ask_wall and self.nearest_ask_wall_distance_pct < 0.5
    
    def get_combined_flow_signal(self) -> float:
        """
        Combine tick flow and order book imbalance for stronger signal.
        
        Returns:
            Combined signal from -1 (bearish) to +1 (bullish)
        """
        if not self.ob_available:
            return self.tick_flow
        
        # Weight: 40% tick flow, 40% OB imbalance, 20% OB trend
        combined = (
            self.tick_flow * 0.4 +
            self.ob_depth_imbalance * 0.4 +
            self.ob_imbalance_trend * 10.0 * 0.2
        )
        
        return max(-1.0, min(1.0, combined))
    
    def to_dict(self) -> Dict[str, Any]:
        """Export metrics as dictionary."""
        result = {}
        for field_obj in self.__dataclass_fields__.values():
            name = field_obj.name
            value = getattr(self, name)
            
            if isinstance(value, Enum):
                result[name] = value.value
            elif isinstance(value, list):
                # Skip patterns list for serialization
                continue
            elif isinstance(value, DetectedPattern):
                result[name] = {
                    'pattern': value.pattern.value,
                    'direction': value.direction,
                    'strength': value.strength,
                }
            else:
                result[name] = value
        
        return result


# ============================================================
# BAND DATA
# ============================================================

@dataclass(slots=True)
class BandData:
    """
    Price band with validation and utility methods.
    
    Represents the predicted price range for strategy evaluation.
    """
    
    low: float
    high: float
    
    def __post_init__(self) -> None:
        """Validate band parameters."""
        if self.low <= 0:
            raise ValueError(f"Band low must be positive: {self.low}")
        if self.high <= 0:
            raise ValueError(f"Band high must be positive: {self.high}")
        if self.high <= self.low:
            raise ValueError(f"Band high must be > low: {self.high} <= {self.low}")
    
    @property
    def range(self) -> float:
        """Absolute price range of band."""
        return self.high - self.low
    
    @property
    def mid(self) -> float:
        """Mid-point of band."""
        return (self.low + self.high) / 2.0
    
    @property
    def width_pct(self) -> float:
        """Band width as percentage of low price."""
        return (self.range / self.low) * 100.0
    
    @property
    def width_bps(self) -> float:
        """Band width in basis points."""
        return self.width_pct * 100.0
    
    def position_in_band(self, price: float) -> float:
        """
        Get position of price within band (0.0 = low, 1.0 = high).
        
        Can return values outside [0, 1] if price is outside band.
        """
        if self.range <= 0:
            return 0.5
        return (price - self.low) / self.range
    
    def price_at_position(self, position: float) -> float:
        """Get price at position within band."""
        position = max(0.0, min(1.0, position))
        return self.low + self.range * position
    
    def is_price_in_band(self, price: float) -> bool:
        """Check if price is within band bounds."""
        return self.low <= price <= self.high
    
    def distance_to_low(self, price: float) -> float:
        """Normalized distance from price to band low."""
        if self.range <= 0:
            return 0.5
        return (price - self.low) / self.range
    
    def distance_to_high(self, price: float) -> float:
        """Normalized distance from price to band high."""
        if self.range <= 0:
            return 0.5
        return (self.high - price) / self.range
    
    def clamp_price(self, price: float) -> float:
        """Clamp price to band bounds."""
        return max(self.low, min(self.high, price))
    
    def expand(self, pct: float) -> 'BandData':
        """Create expanded band by percentage."""
        expansion = self.range * pct / 2.0
        return BandData(
            low=self.low - expansion,
            high=self.high + expansion,
        )
    
    def to_dict(self) -> Dict[str, float]:
        """Export as dictionary."""
        return {
            'low': self.low,
            'high': self.high,
            'range': self.range,
            'mid': self.mid,
            'width_pct': self.width_pct,
        }


# ============================================================
# ORDER BOOK WRAPPER (Optimized with Caching)
# ============================================================

class OrderBookWrapper:
    """
    Unified wrapper for order book access with result caching.
    
    Provides consistent interface whether using:
    - Real OrderBookAnalyzer from live trading
    - Synthetic OrderBookAnalyzer from backtesting
    - Simple dict-based metrics
    
    All expensive calculations are cached on first access.
    """
    
    __slots__ = (
        '_analyzer',
        '_current',
        # Capability flags (computed once)
        '_has_current',
        '_has_imbalance',
        '_has_depth_imbalance',
        '_has_spread_pct',
        '_has_imbalance_trend',
        '_has_is_bullish',
        '_has_is_bearish',
        '_has_bid_wall',
        '_has_ask_wall',
        '_has_total_bid_volume',
        '_has_total_ask_volume',
        '_has_microprice',
        '_has_weighted_mid',
        '_has_find_walls',
        '_has_market_impact',
        '_has_bids_asks',
        # Cached values
        '_cache_imbalance',
        '_cache_depth_imbalance',
        '_cache_spread_pct',
        '_cache_imbalance_trend',
        '_cache_microprice',
        '_cache_weighted_mid',
        '_cache_depth_gradient',
        '_cache_bid_volume',
        '_cache_ask_volume',
        '_cache_walls',
    )
    
    def __init__(self, analyzer: Any) -> None:
        """Initialize wrapper with analyzer or None."""
        self._analyzer = analyzer
        self._current = None
        
        # Get current snapshot once
        if analyzer is not None and hasattr(analyzer, 'current'):
            self._current = analyzer.current
            self._has_current = self._current is not None
        else:
            self._has_current = False
        
        # Cache capability checks (avoid repeated hasattr)
        if self._has_current and self._current is not None:
            current = self._current
            self._has_imbalance = hasattr(current, 'imbalance')
            self._has_depth_imbalance = hasattr(current, 'depth_imbalance')
            self._has_spread_pct = hasattr(current, 'spread_pct')
            self._has_total_bid_volume = hasattr(current, 'total_bid_volume')
            self._has_total_ask_volume = hasattr(current, 'total_ask_volume')
            self._has_microprice = hasattr(current, 'microprice')
            self._has_weighted_mid = hasattr(current, 'weighted_mid_price')
            self._has_find_walls = hasattr(current, 'find_walls')
            self._has_market_impact = hasattr(current, 'market_impact')
            self._has_bids_asks = hasattr(current, 'bids') and hasattr(current, 'asks')
        else:
            self._has_imbalance = False
            self._has_depth_imbalance = False
            self._has_spread_pct = False
            self._has_total_bid_volume = False
            self._has_total_ask_volume = False
            self._has_microprice = False
            self._has_weighted_mid = False
            self._has_find_walls = False
            self._has_market_impact = False
            self._has_bids_asks = False
        
        # Analyzer-level capabilities
        if analyzer is not None:
            self._has_imbalance_trend = hasattr(analyzer, 'imbalance_trend')
            self._has_is_bullish = hasattr(analyzer, 'is_bullish')
            self._has_is_bearish = hasattr(analyzer, 'is_bearish')
            self._has_bid_wall = hasattr(analyzer, 'has_bid_wall_near')
            self._has_ask_wall = hasattr(analyzer, 'has_ask_wall_near')
        else:
            self._has_imbalance_trend = False
            self._has_is_bullish = False
            self._has_is_bearish = False
            self._has_bid_wall = False
            self._has_ask_wall = False
        
        # Initialize cache slots
        self._cache_imbalance: Optional[float] = None
        self._cache_depth_imbalance: Optional[float] = None
        self._cache_spread_pct: Optional[float] = None
        self._cache_imbalance_trend: Optional[float] = None
        self._cache_microprice: Optional[float] = None
        self._cache_weighted_mid: Optional[float] = None
        self._cache_depth_gradient: Optional[Tuple[float, float]] = None
        self._cache_bid_volume: Optional[Dict[int, float]] = None
        self._cache_ask_volume: Optional[Dict[int, float]] = None
        self._cache_walls: Optional[Dict[str, List]] = None
    
    @property
    def is_available(self) -> bool:
        """Check if order book data is available and valid."""
        if not self._has_current or self._current is None:
            return False
        
        # Check validity if method exists
        if hasattr(self._current, 'is_valid'):
            return bool(self._current.is_valid)
        
        # Fallback: check for best bid/ask
        if hasattr(self._current, 'best_bid') and hasattr(self._current, 'best_ask'):
            return self._current.best_bid > 0 and self._current.best_ask > 0
        
        return False
    
    @property
    def is_stale(self) -> bool:
        """Check if order book data is stale."""
        if not self.is_available:
            return True
        
        if hasattr(self._current, 'is_stale'):
            try:
                return self._current.is_stale()
            except TypeError:
                return self._current.is_stale
        
        return False
    
    @property
    def current(self) -> Optional[Any]:
        """Get current order book snapshot."""
        return self._current
    
    @property
    def mid_price(self) -> float:
        """Get mid price."""
        if not self.is_available:
            return 0.0
        if hasattr(self._current, 'mid_price'):
            return float(self._current.mid_price)
        return 0.0
    
    @property
    def best_bid(self) -> float:
        """Get best bid price."""
        if not self.is_available:
            return 0.0
        if hasattr(self._current, 'best_bid'):
            return float(self._current.best_bid)
        return 0.0
    
    @property
    def best_ask(self) -> float:
        """Get best ask price."""
        if not self.is_available:
            return 0.0
        if hasattr(self._current, 'best_ask'):
            return float(self._current.best_ask)
        return 0.0
    
    @property
    def best_bid_size(self) -> float:
        """Get size at best bid."""
        if not self.is_available:
            return 0.0
        if hasattr(self._current, 'best_bid_size'):
            return float(self._current.best_bid_size)
        return 0.0
    
    @property
    def best_ask_size(self) -> float:
        """Get size at best ask."""
        if not self.is_available:
            return 0.0
        if hasattr(self._current, 'best_ask_size'):
            return float(self._current.best_ask_size)
        return 0.0
    
    def get_imbalance(self) -> float:
        """Get top-of-book imbalance with caching."""
        if self._cache_imbalance is not None:
            return self._cache_imbalance
        
        if not self._has_imbalance or self._current is None:
            self._cache_imbalance = 0.0
            return 0.0
        
        try:
            result = float(self._current.imbalance)
        except (TypeError, AttributeError):
            result = 0.0
        
        self._cache_imbalance = result
        return result
    
    def get_depth_imbalance(self, levels: int = 10) -> float:
        """Get multi-level depth imbalance with caching."""
        if self._cache_depth_imbalance is not None:
            return self._cache_depth_imbalance
        
        if not self._has_depth_imbalance or self._current is None:
            result = self.get_imbalance()
            self._cache_depth_imbalance = result
            return result
        
        try:
            result = float(self._current.depth_imbalance(levels))
        except TypeError:
            try:
                result = float(self._current.depth_imbalance)
            except (TypeError, AttributeError):
                result = self.get_imbalance()
        
        self._cache_depth_imbalance = result
        return result
    
    def get_spread_pct(self) -> float:
        """Get spread as percentage with caching."""
        if self._cache_spread_pct is not None:
            return self._cache_spread_pct
        
        if not self._has_spread_pct or self._current is None:
            self._cache_spread_pct = 0.0
            return 0.0
        
        try:
            result = float(self._current.spread_pct)
        except (TypeError, AttributeError):
            result = 0.0
        
        self._cache_spread_pct = result
        return result
    
    def get_spread_bps(self) -> float:
        """Get spread in basis points."""
        return self.get_spread_pct() * 100.0
    
    def get_microprice(self) -> float:
        """Get microprice (size-weighted fair price) with caching."""
        if self._cache_microprice is not None:
            return self._cache_microprice
        
        if not self._has_microprice or self._current is None:
            self._cache_microprice = self.mid_price
            return self._cache_microprice
        
        try:
            result = float(self._current.microprice())
        except (TypeError, AttributeError):
            result = self.mid_price
        
        self._cache_microprice = result
        return result
    
    def get_weighted_mid(self, levels: int = 5) -> float:
        """Get volume-weighted mid price with caching."""
        if self._cache_weighted_mid is not None:
            return self._cache_weighted_mid
        
        if not self._has_weighted_mid or self._current is None:
            self._cache_weighted_mid = self.mid_price
            return self._cache_weighted_mid
        
        try:
            result = float(self._current.weighted_mid_price(levels))
        except (TypeError, AttributeError):
            result = self.mid_price
        
        self._cache_weighted_mid = result
        return result
    
    def get_imbalance_trend(self, window: int = 20) -> float:
        """Get imbalance trend (slope) with caching."""
        if self._cache_imbalance_trend is not None:
            return self._cache_imbalance_trend
        
        if not self._has_imbalance_trend or self._analyzer is None:
            self._cache_imbalance_trend = 0.0
            return 0.0
        
        try:
            result = float(self._analyzer.imbalance_trend(window))
        except TypeError:
            try:
                result = float(self._analyzer.imbalance_trend())
            except (TypeError, AttributeError):
                result = 0.0
        
        self._cache_imbalance_trend = result
        return result
    
    def is_spread_acceptable(self, max_spread_pct: float = 0.15) -> bool:
        """Check if spread is within acceptable threshold."""
        if not self._has_spread_pct:
            return True
        return self.get_spread_pct() <= max_spread_pct
    
    def is_bullish(
        self,
        imbalance_threshold: float = 0.2,
        trend_threshold: float = 0.01,
        require_both: bool = False,
    ) -> bool:
        """Check for bullish order book conditions."""
        if self._has_is_bullish:
            try:
                return self._analyzer.is_bullish(
                    imbalance_threshold,
                    trend_threshold,
                    require_both,
                )
            except TypeError:
                pass
        
        # Fallback calculation
        imbalance_ok = self.get_imbalance() > imbalance_threshold
        trend_ok = self.get_imbalance_trend() > trend_threshold
        
        if require_both:
            return imbalance_ok and trend_ok
        return imbalance_ok or trend_ok
    
    def is_bearish(
        self,
        imbalance_threshold: float = 0.2,
        trend_threshold: float = 0.01,
        require_both: bool = False,
    ) -> bool:
        """Check for bearish order book conditions."""
        if self._has_is_bearish:
            try:
                return self._analyzer.is_bearish(
                    imbalance_threshold,
                    trend_threshold,
                    require_both,
                )
            except TypeError:
                pass
        
        # Fallback calculation
        imbalance_ok = self.get_imbalance() < -imbalance_threshold
        trend_ok = self.get_imbalance_trend() < -trend_threshold
        
        if require_both:
            return imbalance_ok and trend_ok
        return imbalance_ok or trend_ok
    
    def has_bid_wall_near(
        self,
        price: float,
        tolerance_pct: float = 0.5,
        threshold_mult: float = 3.0,
    ) -> bool:
        """Check for bid wall (support) near price."""
        if self._has_bid_wall:
            try:
                return self._analyzer.has_bid_wall_near(price, tolerance_pct)
            except Exception:
                pass
        
        # Fallback: check walls directly
        walls = self.get_walls(threshold_mult)
        if not walls.get('bid_walls'):
            return False
        
        tolerance = price * (tolerance_pct / 100.0)
        return any(abs(w.price - price) <= tolerance for w in walls['bid_walls'])
    
    def has_ask_wall_near(
        self,
        price: float,
        tolerance_pct: float = 0.5,
        threshold_mult: float = 3.0,
    ) -> bool:
        """Check for ask wall (resistance) near price."""
        if self._has_ask_wall:
            try:
                return self._analyzer.has_ask_wall_near(price, tolerance_pct)
            except Exception:
                pass
        
        # Fallback: check walls directly
        walls = self.get_walls(threshold_mult)
        if not walls.get('ask_walls'):
            return False
        
        tolerance = price * (tolerance_pct / 100.0)
        return any(abs(w.price - price) <= tolerance for w in walls['ask_walls'])
    
    def get_walls(self, threshold_mult: float = 3.0) -> Dict[str, List]:
        """Get bid and ask walls with caching."""
        if self._cache_walls is not None:
            return self._cache_walls
        
        if not self._has_find_walls or self._current is None:
            self._cache_walls = {'bid_walls': [], 'ask_walls': []}
            return self._cache_walls
        
        try:
            result = self._current.find_walls(threshold_mult)
        except (TypeError, AttributeError):
            result = {'bid_walls': [], 'ask_walls': []}
        
        self._cache_walls = result
        return result
    
    def get_total_bid_volume(self, levels: int = 10) -> float:
        """Get total bid volume across levels."""
        if self._cache_bid_volume is None:
            self._cache_bid_volume = {}
        
        if levels in self._cache_bid_volume:
            return self._cache_bid_volume[levels]
        
        if not self._has_total_bid_volume or self._current is None:
            self._cache_bid_volume[levels] = 0.0
            return 0.0
        
        try:
            result = float(self._current.total_bid_volume(levels))
        except (TypeError, AttributeError):
            result = 0.0
        
        self._cache_bid_volume[levels] = result
        return result
    
    def get_total_ask_volume(self, levels: int = 10) -> float:
        """Get total ask volume across levels."""
        if self._cache_ask_volume is None:
            self._cache_ask_volume = {}
        
        if levels in self._cache_ask_volume:
            return self._cache_ask_volume[levels]
        
        if not self._has_total_ask_volume or self._current is None:
            self._cache_ask_volume[levels] = 0.0
            return 0.0
        
        try:
            result = float(self._current.total_ask_volume(levels))
        except (TypeError, AttributeError):
            result = 0.0
        
        self._cache_ask_volume[levels] = result
        return result
    
    def get_depth_gradient(self, levels: int = 10) -> Tuple[float, float]:
        """
        Calculate depth gradient (density change across levels) with caching.
        
        Returns:
            (bid_gradient, ask_gradient)
            Negative gradient = thin at top, thick below (support/resistance building)
            Positive gradient = thick at top, thin below (support/resistance weakening)
        """
        if self._cache_depth_gradient is not None:
            return self._cache_depth_gradient
        
        if not self._has_bids_asks or self._current is None:
            self._cache_depth_gradient = (0.0, 0.0)
            return self._cache_depth_gradient
        
        try:
            bids = self._current.bids
            asks = self._current.asks
        except AttributeError:
            self._cache_depth_gradient = (0.0, 0.0)
            return self._cache_depth_gradient
        
        def calc_gradient(levels_list: List) -> float:
            if not levels_list or len(levels_list) < 4:
                return 0.0
            
            n = min(levels, len(levels_list))
            
            # Get sizes
            try:
                sizes = [float(level.size) for level in levels_list[:n]]
            except (AttributeError, TypeError):
                return 0.0
            
            if len(sizes) < 4:
                return 0.0
            
            # Compare top half vs bottom half
            half = n // 2
            top_avg = sum(sizes[:half]) / half if half > 0 else 0
            bottom_avg = sum(sizes[half:n]) / (n - half) if (n - half) > 0 else 0
            
            total = top_avg + bottom_avg
            if total <= 0:
                return 0.0
            
            # Positive = thick at top, negative = thick at bottom
            return (top_avg - bottom_avg) / total
        
        try:
            bid_grad = calc_gradient(bids)
            ask_grad = calc_gradient(asks)
            result = (bid_grad, ask_grad)
        except Exception:
            result = (0.0, 0.0)
        
        self._cache_depth_gradient = result
        return result
    
    def get_market_impact(
        self,
        size: float,
        side: str = "buy",
    ) -> float:
        """Estimate fill price for market order."""
        if not self._has_market_impact or self._current is None:
            return self.mid_price
        
        try:
            return float(self._current.market_impact(size, side))
        except (TypeError, AttributeError):
            return self.mid_price
    
    def get_queue_imbalance(self) -> Tuple[float, float]:
        """
        Get top-of-book vs deep imbalance for queue analysis.
        
        Returns:
            (top_imbalance, deep_imbalance)
        """
        top = self.get_imbalance()
        deep = self.get_depth_imbalance(10)
        return top, deep
    
    def get_value_imbalance(self, levels: int = 10) -> float:
        """Notional value imbalance (bid_value - ask_value) / total."""
        bid_val = self.get_total_bid_volume(levels) * self.best_bid
        ask_val = self.get_total_ask_volume(levels) * self.best_ask
        total = bid_val + ask_val
        if total <= 0:
            return 0.0
        return (bid_val - ask_val) / total
    
    def get_liquidity_ratio(self, levels: int = 10) -> float:
        """Bid/ask liquidity ratio. >1 = more bid support."""
        bid_vol = self.get_total_bid_volume(levels)
        ask_vol = self.get_total_ask_volume(levels)
        if ask_vol <= 0:
            return 2.0 if bid_vol > 0 else 1.0
        return bid_vol / ask_vol
    
    def get_wall_distances(self):
        """Get (bid_wall_dist_pct, ask_wall_dist_pct) from mid price."""
        mid = self.mid_price
        if mid <= 0:
            return 999.0, 999.0
        
        bid_dist, ask_dist = 999.0, 999.0
        walls = self.get_walls(3.0)
        
        if walls.get('bid_walls'):
            try:
                bid_dist = abs(mid - walls['bid_walls'][0].price) / mid * 100
            except (AttributeError, IndexError):
                pass
        
        if walls.get('ask_walls'):
            try:
                ask_dist = abs(walls['ask_walls'][0].price - mid) / mid * 100
            except (AttributeError, IndexError):
                pass
        
        return bid_dist, ask_dist
    
    def populate_metrics(self, metrics: PrecomputedMetrics, config: 'StrategyConfig') -> None:
        """Populate PrecomputedMetrics with all order book data."""
        if not self.is_available:
            metrics.ob_available = False
            return
        
        metrics.ob_available = True
        metrics.ob_stale = self.is_stale
        
        # Basic prices
        metrics.ob_mid_price = self.mid_price
        metrics.ob_best_bid = self.best_bid
        metrics.ob_best_ask = self.best_ask
        metrics.ob_best_bid_size = self.best_bid_size
        metrics.ob_best_ask_size = self.best_ask_size
        
        # Imbalances
        metrics.ob_imbalance = self.get_imbalance()
        metrics.ob_depth_imbalance = self.get_depth_imbalance(config.depth_levels)
        metrics.ob_spread_pct = self.get_spread_pct()
        metrics.ob_spread_bps = metrics.ob_spread_pct * 100.0
        metrics.ob_imbalance_trend = self.get_imbalance_trend(20)
        
        # Direction
        imb_threshold = config.min_imbalance_for_entry
        metrics.ob_is_bullish = self.is_bullish(imb_threshold)
        metrics.ob_is_bearish = self.is_bearish(imb_threshold)
        
        # Advanced prices
        metrics.ob_microprice = self.get_microprice()
        metrics.ob_weighted_mid = self.get_weighted_mid(5)
        
        # Volume
        metrics.ob_bid_volume_5 = self.get_total_bid_volume(5)
        metrics.ob_ask_volume_5 = self.get_total_ask_volume(5)
        metrics.ob_bid_volume_10 = self.get_total_bid_volume(10)
        metrics.ob_ask_volume_10 = self.get_total_ask_volume(10)
        
        # Depth gradient
        bid_grad, ask_grad = self.get_depth_gradient(10)
        metrics.bid_depth_gradient = bid_grad
        metrics.ask_depth_gradient = ask_grad
        
        # Walls
        walls = self.get_walls(3.0)
        bid_walls = walls.get('bid_walls', [])
        ask_walls = walls.get('ask_walls', [])
        
        metrics.has_bid_wall = len(bid_walls) > 0
        metrics.has_ask_wall = len(ask_walls) > 0
        
        if bid_walls:
            try:
                metrics.bid_wall_price = float(bid_walls[0].price)
                metrics.bid_wall_size = float(bid_walls[0].size)
            except (AttributeError, IndexError):
                pass
        
        if ask_walls:
            try:
                metrics.ask_wall_price = float(ask_walls[0].price)
                metrics.ask_wall_size = float(ask_walls[0].size)
            except (AttributeError, IndexError):
                pass
        
        # Enhanced OB metrics (v4.1)
        metrics.ob_value_imbalance = self.get_value_imbalance(config.depth_levels)
        metrics.ob_liquidity_ratio = self.get_liquidity_ratio(10)
        bid_dist, ask_dist = self.get_wall_distances()
        metrics.nearest_bid_wall_distance_pct = bid_dist
        metrics.nearest_ask_wall_distance_pct = ask_dist


# ============================================================
# TICK ANALYSIS (Heavily Optimized)
# ============================================================

class TickAnalysis:
    """
    Tick data analysis with precomputation optimization.
    
    All expensive calculations done once in precompute().
    Designed for high-frequency backtesting across many pairs.
    """
    
    __slots__ = (
        'ticks',
        'band',
        'order_book',
        'candle_data',
        'tick_sizes',
        '_prices',
        '_timestamps',
        '_sizes',
        '_ob_wrapper',
        '_pattern_detector',
        '_regime_detector',
        '_metrics',
        '_touched_low',
        '_touched_high',
        '_low_touches',
        '_high_touches',
    )
    
    def __init__(
        self,
        ticks: List[TickTuple],
        band: BandData,
        order_book: Optional[Any] = None,
        candle_data: Optional[pd.DataFrame] = None,
        tick_sizes: Optional[List[float]] = None,
    ) -> None:
        """
        Initialize tick analysis.
        
        Args:
            ticks: List of (timestamp_ms, price) tuples
            band: Price band for context
            order_book: OrderBookAnalyzer or compatible object
            candle_data: Historical candle DataFrame
            tick_sizes: List of trade sizes corresponding to ticks
        """
        if not ticks:
            raise ValueError("Ticks cannot be empty")
        
        self.ticks = ticks
        self.band = band
        self.order_book = order_book
        self.candle_data = candle_data
        self.tick_sizes = tick_sizes
        
        # Convert to numpy arrays for vectorized operations
        n = len(ticks)
        self._timestamps = np.empty(n, dtype=np.int64)
        self._prices = np.empty(n, dtype=np.float64)
        
        for i, (ts, price) in enumerate(ticks):
            self._timestamps[i] = ts
            self._prices[i] = price
        
        # Handle tick sizes
        if tick_sizes is not None and len(tick_sizes) >= n:
            self._sizes = np.array(tick_sizes[:n], dtype=np.float64)
        else:
            self._sizes = np.ones(n, dtype=np.float64)
        
        # Ensure no zero sizes
        self._sizes = np.maximum(self._sizes, 1e-10)
        
        # Initialize wrappers
        self._ob_wrapper = OrderBookWrapper(order_book)
        self._pattern_detector = CandlePatternDetector()
        self._regime_detector = MarketRegimeDetector()
        
        # Analyze touches (fast vectorized)
        self._analyze_touches()
        
        # Metrics computed on demand
        self._metrics: Optional[PrecomputedMetrics] = None
    
    def _analyze_touches(self) -> None:
        """Analyze band touches using vectorized operations."""
        zone_size = self.band.range * StrategyConstants.TOUCH_ZONE_PCT
        low_zone_upper = self.band.low + zone_size
        high_zone_lower = self.band.high - zone_size
        
        # Vectorized touch detection
        in_low_zone = self._prices <= low_zone_upper
        in_high_zone = self._prices >= high_zone_lower
        
        self._touched_low = bool(np.any(in_low_zone))
        self._touched_high = bool(np.any(in_high_zone))
        
        # Count distinct touches (zone transitions)
        self._low_touches = 0
        self._high_touches = 0
        
        if len(self._prices) < 2:
            if self._touched_low:
                self._low_touches = 1
            if self._touched_high:
                self._high_touches = 1
            return
        
        # Track zone transitions
        last_zone: Optional[str] = None
        
        for i in range(len(self._prices)):
            if in_low_zone[i]:
                if last_zone != "low":
                    self._low_touches += 1
                    last_zone = "low"
            elif in_high_zone[i]:
                if last_zone != "high":
                    self._high_touches += 1
                    last_zone = "high"
            else:
                last_zone = None
    
    def precompute(self, config: StrategyConfig) -> PrecomputedMetrics:
        """
        Precompute ALL metrics used by strategies.
        
        This is the main entry point - call once per candle.
        """
        if self._metrics is not None:
            return self._metrics
        
        metrics = PrecomputedMetrics()
        
        # Basic metadata
        metrics.tick_count = len(self._prices)
        metrics.timestamp = int(self._timestamps[-1]) if len(self._timestamps) > 0 else 0
        
        # Basic price metrics
        metrics.last_price = float(self._prices[-1])
        metrics.first_price = float(self._prices[0])
        metrics.min_price = float(np.min(self._prices))
        metrics.max_price = float(np.max(self._prices))
        metrics.price_range = metrics.max_price - metrics.min_price
        
        # Position in band
        metrics.position_in_band = self.band.position_in_band(metrics.last_price)
        metrics.distance_to_low = self.band.distance_to_low(metrics.last_price)
        metrics.distance_to_high = self.band.distance_to_high(metrics.last_price)
        
        # Touch analysis (already computed)
        metrics.touched_low = self._touched_low
        metrics.touched_high = self._touched_high
        metrics.low_touches = self._low_touches
        metrics.high_touches = self._high_touches
        
        # Velocity at multiple windows
        metrics.velocity_5 = self._compute_velocity(5)
        metrics.velocity_10 = self._compute_velocity(10)
        metrics.velocity_20 = self._compute_velocity(20)
        metrics.velocity_30 = self._compute_velocity(30)
        
        # Velocity reversal detection
        reversed_flag, direction = self._check_velocity_reversal(
            config.velocity_window
        )
        metrics.velocity_reversed = reversed_flag
        metrics.reversal_direction = direction
        
        # Volatility at multiple windows
        metrics.volatility_10 = self._compute_volatility(10)
        metrics.volatility_20 = self._compute_volatility(20)
        metrics.volatility_50 = self._compute_volatility(50)
        
        # Volume metrics
        metrics.total_volume = float(np.sum(self._sizes))
        metrics.avg_tick_size = float(np.mean(self._sizes))
        metrics.volume_ratio = self._compute_volume_ratio(config.volume_lookback)
        
        # VWAP metrics
        metrics.vwap = self._compute_vwap()
        if metrics.vwap > 0:
            metrics.vwap_deviation = (metrics.last_price - metrics.vwap) / metrics.vwap
        
        # Large trade detection
        large_threshold = metrics.avg_tick_size * config.institutional_size_mult
        (
            metrics.large_trade_count,
            metrics.large_trade_buy_count,
            metrics.large_trade_sell_count,
            metrics.large_trade_buy_ratio,
            metrics.large_trade_total_volume,
        ) = self._detect_large_trades(large_threshold)
        
        # Tick flow at multiple windows
        metrics.tick_flow = self._compute_tick_flow(20)
        metrics.tick_flow_5 = self._compute_tick_flow(5)
        metrics.tick_flow_10 = self._compute_tick_flow(10)
        metrics.tick_flow_20 = metrics.tick_flow
        
        # Clustering analysis
        metrics.low_cluster_ratio = self._compute_cluster_ratio("low", 15.0)
        metrics.high_cluster_ratio = self._compute_cluster_ratio("high", 15.0)
        
        # Order book metrics (if available)
        self._ob_wrapper.populate_metrics(metrics, config)
        
        # Market regime detection
        metrics.regime = self._regime_detector.detect(
            metrics.volatility_20,
            metrics.velocity_20,
        )
        
        # Patterns are computed lazily
        metrics.patterns_computed = False
        
        self._metrics = metrics
        return metrics
    
    def compute_patterns(self, metrics: PrecomputedMetrics) -> None:
        """
        Compute candle patterns (lazy - call only when needed).
        
        Modifies metrics in place.
        """
        if metrics.patterns_computed:
            return
        
        if self.candle_data is not None and len(self.candle_data) >= 1:
            metrics.patterns = self._pattern_detector.detect_all(self.candle_data)
            
            bullish = [p for p in metrics.patterns if p.is_bullish]
            bearish = [p for p in metrics.patterns if p.is_bearish]
            
            metrics.strongest_bullish_pattern = bullish[0] if bullish else None
            metrics.strongest_bearish_pattern = bearish[0] if bearish else None
        
        metrics.patterns_computed = True
    
    def _compute_velocity(self, window: int) -> float:
        """Compute normalized velocity over window."""
        n = len(self._prices)
        if n < 2:
            return 0.0
        
        actual_window = min(window, n)
        if actual_window < 2:
            return 0.0
        
        prices = self._prices[-actual_window:]
        raw_velocity = (prices[-1] - prices[0]) / (actual_window - 1)
        
        # Normalize by band range
        if self.band.range <= 0:
            return 0.0
        
        return raw_velocity / self.band.range
    
    def _check_velocity_reversal(self, window: int = 5) -> Tuple[bool, str]:
        """Check if velocity changed direction recently."""
        required_length = window * 2
        if len(self._prices) < required_length:
            return False, "none"
        
        prior = self._prices[-(window * 2):-window]
        recent = self._prices[-window:]
        
        prior_velocity = prior[-1] - prior[0]
        recent_velocity = recent[-1] - recent[0]
        
        # Minimum change threshold
        min_change = self.band.range * 0.01
        
        # Was falling, now rising
        if prior_velocity < -min_change and recent_velocity > min_change:
            return True, "up"
        
        # Was rising, now falling
        if prior_velocity > min_change and recent_velocity < -min_change:
            return True, "down"
        
        return False, "none"
    
    def _compute_volatility(self, window: int) -> float:
        """Compute volatility (std of returns) over window."""
        n = len(self._prices)
        if n < 3:
            return 0.0
        
        actual_window = min(window + 1, n)
        if actual_window < 3:
            return 0.0
        
        prices = self._prices[-actual_window:]
        
        with np.errstate(divide='ignore', invalid='ignore'):
            returns = np.diff(prices) / prices[:-1]
            returns = returns[np.isfinite(returns)]
        
        if len(returns) < 2:
            return 0.0
        
        return float(np.std(returns))
    
    def _compute_tick_flow(self, window: int) -> float:
        """
        Compute buy/sell imbalance using tick rule (vectorized).
        
        Returns value from -1 (all sells) to +1 (all buys).
        """
        n = len(self._prices)
        if n < 3:
            return 0.0
        
        actual_window = min(window, n)
        if actual_window < 3:
            return 0.0
        
        prices = self._prices[-actual_window:]
        
        # Handle size array properly
        if len(self._sizes) >= len(self._prices):
            sizes = self._sizes[-actual_window:]
        else:
            # Pad sizes to match prices
            sizes = np.ones(actual_window, dtype=np.float64)
            available = min(len(self._sizes), actual_window)
            if available > 0:
                sizes[-available:] = self._sizes[-available:]
        
        # Classify ticks by price movement
        price_diff = np.diff(prices)
        sizes_for_moves = sizes[1:]
        
        # Buy = uptick, Sell = downtick
        buy_mask = price_diff > 0
        sell_mask = price_diff < 0
        unchanged_mask = ~buy_mask & ~sell_mask
        
        buy_volume = float(np.sum(sizes_for_moves[buy_mask]))
        sell_volume = float(np.sum(sizes_for_moves[sell_mask]))
        
        # Split unchanged volume equally
        unchanged_volume = float(np.sum(sizes_for_moves[unchanged_mask])) / 2.0
        buy_volume += unchanged_volume
        sell_volume += unchanged_volume
        
        total = buy_volume + sell_volume
        if total <= 0:
            return 0.0
        
        return (buy_volume - sell_volume) / total
    
    def _compute_cluster_ratio(self, zone: str, zone_pct: float) -> float:
        """Compute ratio of volume in a zone."""
        if len(self._prices) == 0:
            return 0.0
        
        zone_size = self.band.range * (zone_pct / 100.0)
        
        if zone == "low":
            zone_limit = self.band.low + zone_size
            mask = self._prices <= zone_limit
        else:
            zone_limit = self.band.high - zone_size
            mask = self._prices >= zone_limit
        
        zone_volume = float(np.sum(self._sizes[mask]))
        total_volume = float(np.sum(self._sizes))
        
        return zone_volume / total_volume if total_volume > 0 else 0.0
    
    def _compute_volume_ratio(self, lookback: int = 20) -> float:
        """Get current volume relative to historical average."""
        if self.candle_data is None or len(self.candle_data) < lookback + 1:
            return 1.0
        
        if 'volume' not in self.candle_data.columns:
            return 1.0
        
        try:
            current_vol = float(self.candle_data['volume'].iloc[-1])
            hist_vol = self.candle_data['volume'].iloc[-(lookback + 1):-1]
            avg_vol = float(hist_vol.mean())
            
            if avg_vol <= 0:
                return 1.0
            
            return current_vol / avg_vol
        except Exception:
            return 1.0
    
    def _compute_vwap(self) -> float:
        """Compute VWAP from ticks."""
        total_volume = float(np.sum(self._sizes))
        if total_volume <= 0:
            return float(self._prices[-1])
        
        return float(np.sum(self._prices * self._sizes) / total_volume)
    
    def _detect_large_trades(
        self,
        threshold: float,
    ) -> Tuple[int, int, int, float, float]:
        """
        Detect large trades and classify by direction.
        
        Returns:
            (total_count, buy_count, sell_count, buy_ratio, total_volume)
        """
        if len(self._prices) < 3:
            return 0, 0, 0, 0.5, 0.0
        
        large_mask = self._sizes >= threshold
        large_count = int(np.sum(large_mask))
        
        if large_count == 0:
            return 0, 0, 0, 0.5, 0.0
        
        # Classify by price direction
        price_diff = np.zeros(len(self._prices))
        price_diff[1:] = np.diff(self._prices)
        
        large_buy_mask = large_mask & (price_diff > 0)
        large_sell_mask = large_mask & (price_diff < 0)
        
        buy_count = int(np.sum(large_buy_mask))
        sell_count = int(np.sum(large_sell_mask))
        
        total_classified = buy_count + sell_count
        buy_ratio = float(buy_count / total_classified) if total_classified > 0 else 0.5
        
        total_volume = float(np.sum(self._sizes[large_mask]))
        
        return large_count, buy_count, sell_count, buy_ratio, total_volume
    
    # Convenience accessors
    @property
    def last_price(self) -> float:
        return float(self._prices[-1])
    
    @property
    def tick_count(self) -> int:
        return len(self._prices)
    
    @property
    def prices(self) -> np.ndarray:
        return self._prices
    
    @property
    def sizes(self) -> np.ndarray:
        return self._sizes
    
    @property
    def timestamps(self) -> np.ndarray:
        return self._timestamps


# ============================================================
# CONFIDENCE CALCULATOR
# ============================================================

class ConfidenceCalculator:
    """Standardized confidence scoring utilities."""
    
    @staticmethod
    def calculate(
        base_strength: float,
        confirmations: List[Tuple[bool, float]],
        min_conf: float = StrategyConstants.MIN_CONFIDENCE,
        max_conf: float = StrategyConstants.MAX_CONFIDENCE,
    ) -> float:
        """
        Calculate confidence with confirmations.
        
        Args:
            base_strength: Base signal strength (0-1)
            confirmations: List of (condition_met, boost) tuples
            min_conf: Minimum confidence floor
            max_conf: Maximum confidence ceiling
            
        Returns:
            Confidence value clamped to [min_conf, max_conf]
        """
        # Start from base confidence plus scaled strength
        conf = StrategyConstants.BASE_CONFIDENCE + base_strength * 0.15
        
        # Apply confirmations
        for condition_met, boost in confirmations:
            if condition_met:
                conf += boost
        
        return max(min_conf, min(max_conf, conf))
    
    @staticmethod
    def apply_regime_adjustment(
        confidence: float,
        regime: MarketRegime,
        favorable_regimes: List[MarketRegime],
        regime_boost: float = StrategyConstants.OB_CONFIRMATION_BOOST,
        regime_penalty: float = StrategyConstants.UNFAVORABLE_REGIME_PENALTY,
    ) -> float:
        """Apply regime-based confidence adjustment."""
        if not favorable_regimes:
            return confidence
        
        if regime in favorable_regimes:
            return min(StrategyConstants.MAX_CONFIDENCE, confidence + regime_boost)
        elif regime != MarketRegime.UNKNOWN:
            return max(StrategyConstants.MIN_CONFIDENCE, confidence - regime_penalty)
        
        return confidence
    
    @staticmethod
    def apply_pattern_adjustment(
        confidence: float,
        action: TradeAction,
        patterns: List[DetectedPattern],
        boost: float = StrategyConstants.PATTERN_CONFIRMATION_BOOST,
        penalty: float = StrategyConstants.CONTRADICTING_PATTERN_PENALTY,
    ) -> Tuple[float, Optional[str]]:
        """
        Apply candle pattern adjustment.
        
        Returns:
            (adjusted_confidence, warning_message_or_none)
        """
        if not patterns:
            return confidence, None
        
        # Check for confirming patterns
        confirming = [p for p in patterns if p.supports_direction(action) and p.strength > 0.5]
        if confirming:
            confidence = min(StrategyConstants.MAX_CONFIDENCE, confidence + boost)
            return confidence, None
        
        # Check for contradicting patterns
        contradicting = [p for p in patterns if p.contradicts_direction(action) and p.strength > 0.6]
        if contradicting:
            confidence = max(StrategyConstants.MIN_CONFIDENCE, confidence - penalty)
            return confidence, f"Contradicting {contradicting[0].pattern.value}"
        
        return confidence, None
    
    @staticmethod
    def apply_orderbook_adjustment(
        confidence: float,
        metrics: PrecomputedMetrics,
        action: TradeAction,
        boost: float = StrategyConstants.OB_CONFIRMATION_BOOST,
    ) -> float:
        """Apply order book confirmation adjustment."""
        if not metrics.ob_available:
            return confidence
        
        if action == TradeAction.LONG:
            if metrics.ob_is_bullish and metrics.ob_imbalance > 0.2:
                return min(StrategyConstants.MAX_CONFIDENCE, confidence + boost)
        elif action == TradeAction.SHORT:
            if metrics.ob_is_bearish and metrics.ob_imbalance < -0.2:
                return min(StrategyConstants.MAX_CONFIDENCE, confidence + boost)
        
        return confidence


# ============================================================
# TRADE SIGNAL
# ============================================================

@dataclass
class TradeSignal:
    """
    Trading signal with complete metadata.
    
    Contains all information needed for execution and analysis.
    """
    
    strategy: str
    action: TradeAction
    entry_price: float
    exit_price: float
    stop_loss: Optional[float] = None
    confidence: float = 0.5
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    position_size: float = 0.1
    
    def __post_init__(self) -> None:
        """Clamp confidence to valid range."""
        self.confidence = max(0.0, min(1.0, self.confidence))
    
    @property
    def action_str(self) -> str:
        """Get action as string."""
        return self.action.value
    
    @property
    def is_long(self) -> bool:
        """Check if long trade."""
        return self.action == TradeAction.LONG
    
    @property
    def is_short(self) -> bool:
        """Check if short trade."""
        return self.action == TradeAction.SHORT
    
    @property
    def expected_pnl_pct(self) -> float:
        """Expected PnL as percentage of entry."""
        if self.entry_price <= 0:
            return 0.0
        
        if self.action == TradeAction.LONG:
            return (self.exit_price - self.entry_price) / self.entry_price
        elif self.action == TradeAction.SHORT:
            return (self.entry_price - self.exit_price) / self.entry_price
        
        return 0.0
    
    @property
    def risk_pct(self) -> float:
        """Risk as percentage of entry (distance to stop)."""
        if self.entry_price <= 0 or self.stop_loss is None:
            return 0.0
        return abs(self.entry_price - self.stop_loss) / self.entry_price
    
    @property
    def reward_risk_ratio(self) -> float:
        """Reward to risk ratio."""
        risk = self.risk_pct
        if risk <= 0:
            return float('inf') if self.expected_pnl_pct > 0 else 0.0
        return abs(self.expected_pnl_pct) / risk
    
    def is_valid(self) -> bool:
        """Check if signal is valid for execution."""
        if self.action == TradeAction.NONE:
            return False
        
        if self.entry_price <= 0 or self.exit_price <= 0:
            return False
        
        if self.action == TradeAction.LONG:
            return self.exit_price > self.entry_price
        else:
            return self.exit_price < self.entry_price
    
    def to_dict(self) -> Dict[str, Any]:
        """Export signal as dictionary."""
        rr = self.reward_risk_ratio
        return {
            'strategy': self.strategy,
            'action': self.action.value,
            'entry_price': round(self.entry_price, 8),
            'exit_price': round(self.exit_price, 8),
            'stop_loss': round(self.stop_loss, 8) if self.stop_loss else None,
            'confidence': round(self.confidence, 4),
            'reason': self.reason,
            'metadata': self.metadata,
            'timestamp': self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            'position_size': round(self.position_size, 4),
            'expected_pnl_pct': round(self.expected_pnl_pct, 6),
            'risk_pct': round(self.risk_pct, 6),
            'reward_risk_ratio': round(rr, 2) if rr != float('inf') else 999.0,
        }
    
    def to_dict_fast(self) -> Dict[str, Any]:
        """Fast serialization without calculated properties."""
        return {
            'strategy': self.strategy,
            'action': self.action.value,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'stop_loss': self.stop_loss,
            'confidence': self.confidence,
            'position_size': self.position_size,
        }


# ============================================================
# POSITION SIZER
# ============================================================

class PositionSizer:
    """
    Position sizing calculator supporting multiple methods.
    
    Thread-safe and stateless for parallel backtesting.
    """
    
    __slots__ = ('config',)
    
    def __init__(self, config: StrategyConfig) -> None:
        """Initialize with strategy configuration."""
        self.config = config
    
    def calculate(
        self,
        method: Optional[PositionSizingMethod] = None,
        win_rate: Optional[float] = None,
        avg_win: Optional[float] = None,
        avg_loss: Optional[float] = None,
        volatility: Optional[float] = None,
        confidence: Optional[float] = None,
    ) -> float:
        """
        Calculate position size based on method.
        
        Args:
            method: Sizing method (defaults to config)
            win_rate: Historical win rate
            avg_win: Average winning trade return
            avg_loss: Average losing trade return (negative)
            volatility: Current volatility
            confidence: Signal confidence
            
        Returns:
            Position size as fraction (0-1)
        """
        method = method or self.config.position_sizing_method
        
        if method == PositionSizingMethod.FIXED:
            return self.config.pos_size
        
        elif method == PositionSizingMethod.KELLY:
            return self._kelly_size(win_rate, avg_win, avg_loss)
        
        elif method == PositionSizingMethod.VOLATILITY_BASED:
            return self._volatility_size(volatility)
        
        elif method == PositionSizingMethod.ADAPTIVE:
            return self._adaptive_size(win_rate, avg_win, avg_loss, volatility, confidence)
        
        return self.config.pos_size
    
    def _kelly_size(
        self,
        win_rate: Optional[float],
        avg_win: Optional[float],
        avg_loss: Optional[float],
    ) -> float:
        """
        Kelly criterion: f* = (p*b - q) / b
        
        Where:
        - p = probability of winning
        - q = probability of losing (1 - p)
        - b = win/loss ratio
        """
        # Use defaults if not provided
        p = win_rate if win_rate is not None else self.config.default_win_rate
        p = max(0.01, min(0.99, p))
        
        # Calculate win/loss ratio
        if avg_win is not None and avg_loss is not None and abs(avg_loss) > 1e-10:
            b = abs(avg_win / avg_loss)
        else:
            b = self.config.default_win_loss_ratio
        
        b = max(0.1, min(10.0, b))
        
        q = 1.0 - p
        
        # Kelly formula
        kelly = (p * b - q) / b
        
        if kelly <= 0:
            return self.config.min_position_size
        
        # Apply fraction to full Kelly
        size = kelly * self.config.kelly_fraction
        return self._clamp_size(size)
    
    def _volatility_size(self, volatility: Optional[float]) -> float:
        """Size inversely proportional to volatility."""
        if volatility is None or volatility <= 0:
            return self.config.pos_size
        
        # Target volatility divided by actual
        size = self.config.target_volatility / volatility
        return self._clamp_size(size)
    
    def _adaptive_size(
        self,
        win_rate: Optional[float],
        avg_win: Optional[float],
        avg_loss: Optional[float],
        volatility: Optional[float],
        confidence: Optional[float],
    ) -> float:
        """Combine Kelly, volatility, and confidence."""
        # Start with Kelly
        kelly_size = self._kelly_size(win_rate, avg_win, avg_loss)
        
        # Volatility factor
        if volatility is not None and volatility > 0:
            vol_factor = self.config.target_volatility / volatility
            vol_factor = max(0.5, min(2.0, vol_factor))
        else:
            vol_factor = 1.0
        
        # Confidence factor
        if confidence is not None:
            conf_factor = confidence / self.config.high_confidence_threshold
            conf_factor = max(0.5, min(1.5, conf_factor))
        else:
            conf_factor = 1.0
        
        size = kelly_size * vol_factor * conf_factor
        return self._clamp_size(size)
    
    def _clamp_size(self, size: float) -> float:
        """Clamp to configured bounds."""
        return max(
            self.config.min_position_size,
            min(self.config.max_position_size, size)
        )

# ============================================================
# BASE STRATEGY (Optimized for Precomputed Metrics)
# ============================================================

class BaseStrategy(ABC):
    """
    Base strategy class with precomputed metrics support.
    
    All 23 strategies inherit from this and override evaluate().
    
    Key features:
    - Single precomputed metrics source (no duplicate calculations)
    - Order book data fully integrated
    - Regime-aware signal generation
    - Unified validation and signal creation
    - Thread-safe and stateless for parallel execution
    """
    
    # Class attributes - override in subclasses
    code: str = "S0"
    name: str = "Base Strategy"
    description: str = ""
    favorable_regimes: List[MarketRegime] = []
    uses_patterns: bool = False  # Set True if strategy needs candle patterns
    uses_orderbook: bool = True  # Set True if strategy uses order book
    min_ticks_required: int = StrategyConstants.MIN_TICKS_FOR_SIGNAL
    
    __slots__ = ('config', 'position_sizer', '_tick_analysis')
    
    def __init__(self, config: StrategyConfig) -> None:
        """Initialize strategy with configuration."""
        self.config = config
        self.position_sizer = PositionSizer(config)
        self._tick_analysis: Optional[TickAnalysis] = None
    
    def set_tick_analysis(self, tick_analysis: TickAnalysis) -> None:
        """Set tick analysis reference for pattern computation."""
        self._tick_analysis = tick_analysis
    
    def _get_param(self, name: str, default: Optional[float] = None) -> float:
        """Get strategy-specific parameter with fallback."""
        return self.config.get_strategy_param(self.code, name, default)
    
    def _has_min_edge(self, entry: float, exit_price: float) -> bool:
        """Check if trade has minimum required edge over costs."""
        if entry <= 0:
            return False
        edge_pct = abs(exit_price - entry) / entry
        return edge_pct >= self.config.min_edge
    
    def _calculate_stop_loss(
        self,
        entry: float,
        band: BandData,
        is_long: bool,
    ) -> Optional[float]:
        """Calculate stop loss price based on band."""
        if not self.config.use_stop_loss:
            return None
        
        stop_distance = band.range * self.config.stop_loss_band_multiplier
        
        if is_long:
            stop = entry - stop_distance
            # Ensure stop is at least 0.5% away
            return min(stop, entry * 0.995)
        else:
            stop = entry + stop_distance
            return max(stop, entry * 1.005)
    
    def _check_order_book(
        self,
        metrics: PrecomputedMetrics,
        is_long: bool,
    ) -> Tuple[bool, str]:
        """
        Check if order book supports the trade direction.
        
        Returns:
            (is_ok, rejection_reason)
        """
        if not self.config.use_order_book:
            return True, ""
        
        if not metrics.ob_available:
            if self.config.require_order_book:
                return False, "Order book required but not available"
            return True, ""
        
        # Check if stale
        if metrics.ob_stale:
            return False, "Order book data is stale"
        
        # Check spread
        if metrics.ob_spread_pct > self.config.max_spread_pct:
            return False, f"Spread too wide: {metrics.ob_spread_pct:.3f}%"
        
        # Check imbalance direction
        if self.config.use_depth_imbalance:
            imbalance = metrics.ob_depth_imbalance
        else:
            imbalance = metrics.ob_imbalance
        
        threshold = self.config.min_imbalance_for_entry
        
        # For longs, we want positive imbalance (more bids)
        if is_long and imbalance < -threshold:
            return False, f"Order book bearish: imbalance={imbalance:.3f}"
        
        # For shorts, we want negative imbalance (more asks)
        if not is_long and imbalance > threshold:
            return False, f"Order book bullish: imbalance={imbalance:.3f}"
        
        return True, ""
    
    def _check_volume(self, metrics: PrecomputedMetrics) -> Tuple[bool, str]:
        """Check if volume confirms the trade."""
        if not self.config.require_volume_confirmation:
            return True, ""
        
        if metrics.volume_ratio < self.config.min_volume_ratio:
            return False, f"Insufficient volume: {metrics.volume_ratio:.2f}x"
        
        return True, ""
    
    def _calculate_position_size(
        self,
        metrics: PrecomputedMetrics,
        confidence: float,
        win_rate: Optional[float] = None,
    ) -> float:
        """Calculate position size based on metrics and confidence."""
        return self.position_sizer.calculate(
            method=self.config.position_sizing_method,
            win_rate=win_rate,
            volatility=metrics.volatility_20,
            confidence=confidence,
        )
    
    def _build_metadata(
        self,
        metrics: PrecomputedMetrics,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build comprehensive signal metadata."""
        meta = dict(extra) if extra else {}
        
        # Order book data
        if metrics.ob_available:
            meta['ob_imbalance'] = round(metrics.ob_imbalance, 4)
            meta['ob_depth_imbalance'] = round(metrics.ob_depth_imbalance, 4)
            meta['ob_spread_pct'] = round(metrics.ob_spread_pct, 4)
            meta['ob_spread_bps'] = round(metrics.ob_spread_bps, 2)
            meta['ob_imbalance_trend'] = round(metrics.ob_imbalance_trend, 6)
            meta['ob_microprice'] = round(metrics.ob_microprice, 8)
            meta['ob_weighted_mid'] = round(metrics.ob_weighted_mid, 8)
            meta['ob_is_bullish'] = metrics.ob_is_bullish
            meta['ob_is_bearish'] = metrics.ob_is_bearish
            meta['has_bid_wall'] = metrics.has_bid_wall
            meta['has_ask_wall'] = metrics.has_ask_wall
        
        # Volume metrics
        meta['volume_ratio'] = round(metrics.volume_ratio, 2)
        meta['total_volume'] = round(metrics.total_volume, 4)
        
        # Tick statistics
        meta['volatility_20'] = round(metrics.volatility_20, 6)
        meta['tick_count'] = metrics.tick_count
        meta['tick_flow'] = round(metrics.tick_flow, 4)
        
        # VWAP
        meta['vwap'] = round(metrics.vwap, 8)
        meta['vwap_deviation'] = round(metrics.vwap_deviation, 6)
        
        # Large trades
        meta['large_trade_count'] = metrics.large_trade_count
        meta['large_trade_buy_ratio'] = round(metrics.large_trade_buy_ratio, 4)
        
        # Depth gradient
        meta['bid_depth_gradient'] = round(metrics.bid_depth_gradient, 4)
        meta['ask_depth_gradient'] = round(metrics.ask_depth_gradient, 4)
        
        # Market regime
        meta['market_regime'] = metrics.regime.value
        
        # Candle patterns
        if metrics.patterns_computed:
            if metrics.strongest_bullish_pattern:
                meta['bullish_pattern'] = metrics.strongest_bullish_pattern.pattern.value
                meta['bullish_pattern_strength'] = round(
                    metrics.strongest_bullish_pattern.strength, 3
                )
            if metrics.strongest_bearish_pattern:
                meta['bearish_pattern'] = metrics.strongest_bearish_pattern.pattern.value
                meta['bearish_pattern_strength'] = round(
                    metrics.strongest_bearish_pattern.strength, 3
                )
        
        return meta
    
    def _compute_patterns_if_needed(self, metrics: PrecomputedMetrics) -> None:
        """Compute patterns lazily if strategy needs them."""
        if not self.uses_patterns:
            return
        
        if metrics.patterns_computed:
            return
        
        if self._tick_analysis is not None:
            self._tick_analysis.compute_patterns(metrics)
    
    def create_long_signal(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
        reason: str,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        confidence: float = 0.5,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradeSignal]:
        """
        Create validated long signal with all checks.
        
        Returns None if any validation fails.
        """
        # Minimum confidence check
        if confidence < self.config.min_confidence:
            return None
        
        # Order book check
        ob_ok, ob_reason = self._check_order_book(metrics, is_long=True)
        if not ob_ok:
            logger.debug(f"{self.name} long rejected: {ob_reason}")
            return None
        
        # Volume check
        vol_ok, vol_reason = self._check_volume(metrics)
        if not vol_ok:
            logger.debug(f"{self.name} long rejected: {vol_reason}")
            return None
        
        # Set prices
        entry = entry_price if entry_price is not None else metrics.last_price
        exit_px = exit_price if exit_price is not None else band.price_at_position(0.75)
        
        # Validate direction
        if entry <= 0 or exit_px <= entry:
            return None
        
        # Edge check
        if not self._has_min_edge(entry, exit_px):
            return None
        
        # Apply candle pattern adjustment
        if self.config.use_candle_patterns:
            self._compute_patterns_if_needed(metrics)
            confidence, warning = ConfidenceCalculator.apply_pattern_adjustment(
                confidence,
                TradeAction.LONG,
                metrics.patterns,
                self.config.candle_pattern_confidence_boost,
                self.config.candle_pattern_contradiction_penalty,
            )
            if warning:
                reason += f" ({warning})"
        
        # Apply regime adjustment
        if self.config.use_regime_filter and self.favorable_regimes:
            confidence = ConfidenceCalculator.apply_regime_adjustment(
                confidence,
                metrics.regime,
                self.favorable_regimes,
                self.config.regime_boost,
                self.config.regime_penalty,
            )
        
        # Apply order book confirmation boost
        confidence = ConfidenceCalculator.apply_orderbook_adjustment(
            confidence, metrics, TradeAction.LONG
        )
        
        # Final confidence check after adjustments
        if confidence < self.config.min_confidence:
            return None
        
        # Calculate stop loss and position size
        stop = self._calculate_stop_loss(entry, band, is_long=True)
        pos_size = self._calculate_position_size(metrics, confidence)
        
        # Build metadata
        meta = self._build_metadata(metrics, extra_metadata)
        
        signal = TradeSignal(
            strategy=self.name,
            action=TradeAction.LONG,
            entry_price=entry,
            exit_price=exit_px,
            stop_loss=stop,
            confidence=confidence,
            reason=reason,
            metadata=meta,
            position_size=pos_size,
        )
        
        return signal if signal.is_valid() else None
    
    def create_short_signal(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
        reason: str,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        confidence: float = 0.5,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradeSignal]:
        """
        Create validated short signal with all checks.
        
        Returns None if any validation fails.
        """
        # Minimum confidence check
        if confidence < self.config.min_confidence:
            return None
        
        # Order book check
        ob_ok, ob_reason = self._check_order_book(metrics, is_long=False)
        if not ob_ok:
            logger.debug(f"{self.name} short rejected: {ob_reason}")
            return None
        
        # Volume check
        vol_ok, vol_reason = self._check_volume(metrics)
        if not vol_ok:
            logger.debug(f"{self.name} short rejected: {vol_reason}")
            return None
        
        # Set prices
        entry = entry_price if entry_price is not None else metrics.last_price
        exit_px = exit_price if exit_price is not None else band.price_at_position(0.25)
        
        # Validate direction
        if entry <= 0 or exit_px >= entry:
            return None
        
        # Edge check
        if not self._has_min_edge(entry, exit_px):
            return None
        
        # Apply candle pattern adjustment
        if self.config.use_candle_patterns:
            self._compute_patterns_if_needed(metrics)
            confidence, warning = ConfidenceCalculator.apply_pattern_adjustment(
                confidence,
                TradeAction.SHORT,
                metrics.patterns,
                self.config.candle_pattern_confidence_boost,
                self.config.candle_pattern_contradiction_penalty,
            )
            if warning:
                reason += f" ({warning})"
        
        # Apply regime adjustment
        if self.config.use_regime_filter and self.favorable_regimes:
            confidence = ConfidenceCalculator.apply_regime_adjustment(
                confidence,
                metrics.regime,
                self.favorable_regimes,
                self.config.regime_boost,
                self.config.regime_penalty,
            )
        
        # Apply order book confirmation boost
        confidence = ConfidenceCalculator.apply_orderbook_adjustment(
            confidence, metrics, TradeAction.SHORT
        )
        
        # Final confidence check
        if confidence < self.config.min_confidence:
            return None
        
        # Calculate stop loss and position size
        stop = self._calculate_stop_loss(entry, band, is_long=False)
        pos_size = self._calculate_position_size(metrics, confidence)
        
        # Build metadata
        meta = self._build_metadata(metrics, extra_metadata)
        
        signal = TradeSignal(
            strategy=self.name,
            action=TradeAction.SHORT,
            entry_price=entry,
            exit_price=exit_px,
            stop_loss=stop,
            confidence=confidence,
            reason=reason,
            metadata=meta,
            position_size=pos_size,
        )
        
        return signal if signal.is_valid() else None
    
    @abstractmethod
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        """
        Evaluate strategy using precomputed metrics.
        
        Args:
            band: Price band context
            metrics: Pre-computed tick/orderbook metrics
            
        Returns:
            TradeSignal if conditions met, else None
        """
        pass


# ============================================================
# STRATEGY S1: PATH CLEAR
# ============================================================

class S1_PathClear(BaseStrategy):
    """
    S1: Path Clear
    
    Price touched one band edge only - path to opposite is clear.
    If touched low but never high, expect move to upside.
    
    Logic:
    - touched_low AND NOT touched_high → LONG
    - touched_high AND NOT touched_low → SHORT
    - More touches = higher confidence
    
    Favorable regimes: RANGING, LOW_VOLATILITY
    """
    
    code = "S1"
    name = "S1_PathClear"
    description = "Trade when path to opposite band is clear"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.LOW_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        # Need minimum ticks
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        # === LONG: Clear path to upside ===
        if metrics.touched_low_only:
            # Base strength from number of low touches
            base_strength = min(1.0, metrics.low_touches / 3.0)
            
            confirmations = [
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.low_touches >= 2, 0.03),
                (metrics.tick_flow > 0.1, 0.02),
                (metrics.velocity_10 > 0, 0.02),
                (metrics.has_bid_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Path clear to upside ({metrics.low_touches} low touches)",
                confidence=confidence,
                extra_metadata={
                    "low_touches": metrics.low_touches,
                    "high_touches": metrics.high_touches,
                },
            )
        
        # === SHORT: Clear path to downside ===
        if metrics.touched_high_only:
            base_strength = min(1.0, metrics.high_touches / 3.0)
            
            confirmations = [
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.high_touches >= 2, 0.03),
                (metrics.tick_flow < -0.1, 0.02),
                (metrics.velocity_10 < 0, 0.02),
                (metrics.has_ask_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Path clear to downside ({metrics.high_touches} high touches)",
                confidence=confidence,
                extra_metadata={
                    "low_touches": metrics.low_touches,
                    "high_touches": metrics.high_touches,
                },
            )
        
        return None


# ============================================================
# STRATEGY S2: MEAN REVERSION
# ============================================================

class S2_MeanReversion(BaseStrategy):
    """
    S2: Mean Reversion with Optional Velocity Confirmation
    
    Fade extremes when price is at band edges.
    Can require velocity reversal for higher confidence.
    
    Logic:
    - Position < threshold (near low) → LONG
    - Position > (1 - threshold) (near high) → SHORT
    - Velocity reversal confirmation boosts confidence
    
    Favorable regimes: RANGING, HIGH_VOLATILITY
    """
    
    code = "S2"
    name = "S2_MeanReversion"
    description = "Fade band extremes with velocity confirmation"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 15
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        threshold = self._get_param("mean_reversion_threshold", 0.25)
        require_reversal = self.config.require_velocity_reversal
        
        # === LONG: At low end of band ===
        if metrics.position_in_band < threshold:
            # Check velocity reversal requirement
            if require_reversal:
                if not (metrics.velocity_reversed and metrics.reversal_direction == "up"):
                    return None
            
            # Base strength from position extremity
            extremity = (threshold - metrics.position_in_band) / threshold
            base_strength = extremity
            
            confirmations = [
                (
                    metrics.velocity_reversed and metrics.reversal_direction == "up",
                    self._get_param("velocity_reversal_boost", 0.05)
                ),
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (abs(metrics.velocity_10) > 0.01, 0.02),
                (metrics.tick_flow > 0.2, 0.03),
                (metrics.ob_imbalance_trend > 0.005, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Mean reversion from low (pos={metrics.position_in_band:.1%})",
                confidence=confidence,
                extra_metadata={
                    "position_in_band": round(metrics.position_in_band, 4),
                    "velocity_10": round(metrics.velocity_10, 6),
                    "velocity_reversed": metrics.velocity_reversed,
                    "reversal_direction": metrics.reversal_direction,
                    "extremity": round(extremity, 4),
                },
            )
        
        # === SHORT: At high end of band ===
        if metrics.position_in_band > (1 - threshold):
            if require_reversal:
                if not (metrics.velocity_reversed and metrics.reversal_direction == "down"):
                    return None
            
            extremity = (metrics.position_in_band - (1 - threshold)) / threshold
            base_strength = extremity
            
            confirmations = [
                (
                    metrics.velocity_reversed and metrics.reversal_direction == "down",
                    self._get_param("velocity_reversal_boost", 0.05)
                ),
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (abs(metrics.velocity_10) > 0.01, 0.02),
                (metrics.tick_flow < -0.2, 0.03),
                (metrics.ob_imbalance_trend < -0.005, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Mean reversion from high (pos={metrics.position_in_band:.1%})",
                confidence=confidence,
                extra_metadata={
                    "position_in_band": round(metrics.position_in_band, 4),
                    "velocity_10": round(metrics.velocity_10, 6),
                    "velocity_reversed": metrics.velocity_reversed,
                    "reversal_direction": metrics.reversal_direction,
                    "extremity": round(extremity, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S3: MULTI-TOUCH
# ============================================================

class S3_MultiTouch(BaseStrategy):
    """
    S3: Multi-Touch Support/Resistance
    
    Multiple touches at same level = strong support/resistance.
    More touches = higher confidence.
    Allows limited opposite touches for robustness.
    
    Logic:
    - low_touches >= min_touches AND high_touches <= max_opposite → LONG
    - high_touches >= min_touches AND low_touches <= max_opposite → SHORT
    
    Favorable regimes: RANGING
    """
    
    code = "S3"
    name = "S3_MultiTouch"
    description = "Trade strong support/resistance from multiple touches"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True
    min_ticks_required = 15
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        min_touches = int(self._get_param("min_touches", 2))
        max_opposite = int(self._get_param("max_opposite_touches", 1))
        
        # === LONG: Strong support ===
        if (metrics.low_touches >= min_touches and
            metrics.high_touches <= max_opposite):
            
            # Strength based on touch count above minimum
            touch_strength = min(1.0, (metrics.low_touches - min_touches + 1) / 3.0)
            
            confirmations = [
                (metrics.has_bid_wall, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.low_touches >= min_touches + 2, 0.05),
                (metrics.tick_flow > 0, 0.02),
                (metrics.ob_imbalance > 0.15, 0.03),
                (metrics.bid_depth_gradient < -0.1, 0.02),  # Support building
            ]
            
            confidence = ConfidenceCalculator.calculate(touch_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Multi-touch support ({metrics.low_touches} low, {metrics.high_touches} high)",
                confidence=confidence,
                extra_metadata={
                    "low_touches": metrics.low_touches,
                    "high_touches": metrics.high_touches,
                    "min_required": min_touches,
                    "max_opposite": max_opposite,
                },
            )
        
        # === SHORT: Strong resistance ===
        if (metrics.high_touches >= min_touches and
            metrics.low_touches <= max_opposite):
            
            touch_strength = min(1.0, (metrics.high_touches - min_touches + 1) / 3.0)
            
            confirmations = [
                (metrics.has_ask_wall, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.high_touches >= min_touches + 2, 0.05),
                (metrics.tick_flow < 0, 0.02),
                (metrics.ob_imbalance < -0.15, 0.03),
                (metrics.ask_depth_gradient > 0.1, 0.02),  # Resistance building
            ]
            
            confidence = ConfidenceCalculator.calculate(touch_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Multi-touch resistance ({metrics.high_touches} high, {metrics.low_touches} low)",
                confidence=confidence,
                extra_metadata={
                    "low_touches": metrics.low_touches,
                    "high_touches": metrics.high_touches,
                    "min_required": min_touches,
                    "max_opposite": max_opposite,
                },
            )
        
        return None


# ============================================================
# STRATEGY S4: SIMPLE BAND ENTRY (Baseline)
# ============================================================

class S4_SimpleBandEntry(BaseStrategy):
    """
    S4: Simple Band Entry
    
    Basic strategy: buy at low, sell at high.
    This is the baseline - other strategies should outperform this.
    
    Logic:
    - Position < edge_threshold → LONG
    - Position > (1 - edge_threshold) → SHORT
    
    Favorable regimes: All (baseline)
    """
    
    code = "S4"
    name = "S4_SimpleBandEntry"
    description = "Baseline: buy at low, sell at high"
    favorable_regimes = []  # Works in any regime
    uses_orderbook = False  # Minimal requirements
    min_ticks_required = 5
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        edge_threshold = self._get_param("pattern_edge_threshold", 0.25)
        position = metrics.position_in_band
        
        # === LONG: At low of band ===
        if position < edge_threshold:
            base_strength = (edge_threshold - position) / edge_threshold
            
            confirmations = [
                (metrics.ob_imbalance > 0, 0.02),
            ]
            
            # Cap confidence for baseline strategy
            confidence = ConfidenceCalculator.calculate(
                base_strength * 0.5,
                confirmations,
                min_conf=0.50,
                max_conf=0.60,
            )
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Simple band touch low (pos={position:.1%})",
                confidence=confidence,
                extra_metadata={"position_in_band": round(position, 4)},
            )
        
        # === SHORT: At high of band ===
        if position > (1 - edge_threshold):
            base_strength = (position - (1 - edge_threshold)) / edge_threshold
            
            confirmations = [
                (metrics.ob_imbalance < 0, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(
                base_strength * 0.5,
                confirmations,
                min_conf=0.50,
                max_conf=0.60,
            )
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Simple band touch high (pos={position:.1%})",
                confidence=confidence,
                extra_metadata={"position_in_band": round(position, 4)},
            )
        
        return None


# ============================================================
# STRATEGY S5: VOLATILITY SQUEEZE
# ============================================================

class S5_VolatilitySqueeze(BaseStrategy):
    """
    S5: Volatility Squeeze
    
    Low volatility relative to history predicts expansion.
    Trade breakout direction using order book for bias.
    
    Logic:
    - Current vol / Historical vol < threshold → Squeeze detected
    - Use OB to determine breakout direction
    
    Favorable regimes: LOW_VOLATILITY
    """
    
    code = "S5"
    name = "S5_VolatilitySqueeze"
    description = "Trade volatility expansion after squeeze"
    favorable_regimes = [MarketRegime.LOW_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 30
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        # Need both short and long term volatility
        current_vol = metrics.volatility_10
        historical_vol = metrics.volatility_50
        
        if historical_vol <= 0 or current_vol <= 0:
            return None
        
        # Squeeze ratio: low = squeeze in progress
        squeeze_ratio = current_vol / historical_vol
        threshold = self._get_param("volatility_squeeze_ratio", 0.6)
        
        if squeeze_ratio >= threshold:
            return None  # No squeeze detected
        
        position = metrics.position_in_band
        
        # Strength based on how tight the squeeze is
        squeeze_strength = 1.0 - (squeeze_ratio / threshold)
        
        # Use order book for direction
        if metrics.ob_available and not metrics.ob_stale:
            # === LONG: Bullish OB at lower half ===
            if (position < 0.5 and 
                metrics.ob_is_bullish and 
                not metrics.ob_is_bearish):
                
                confirmations = [
                    (True, squeeze_strength * 0.1),  # Squeeze strength bonus
                    (metrics.ob_imbalance_trend > 0, 0.03),
                    (metrics.tick_flow > 0, 0.02),
                    (metrics.bid_depth_gradient < 0, 0.02),  # Support building
                ]
                
                confidence = ConfidenceCalculator.calculate(squeeze_strength, confirmations)
                
                return self.create_long_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Volatility squeeze breakout up (ratio={squeeze_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "vol_ratio": round(squeeze_ratio, 4),
                        "current_vol": round(current_vol, 6),
                        "historical_vol": round(historical_vol, 6),
                        "squeeze_strength": round(squeeze_strength, 4),
                        "ob_confirmed": True,
                    },
                )
            
            # === SHORT: Bearish OB at upper half ===
            if (position > 0.5 and 
                metrics.ob_is_bearish and 
                not metrics.ob_is_bullish):
                
                confirmations = [
                    (True, squeeze_strength * 0.1),
                    (metrics.ob_imbalance_trend < 0, 0.03),
                    (metrics.tick_flow < 0, 0.02),
                    (metrics.ask_depth_gradient > 0, 0.02),  # Resistance building
                ]
                
                confidence = ConfidenceCalculator.calculate(squeeze_strength, confirmations)
                
                return self.create_short_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Volatility squeeze breakout down (ratio={squeeze_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "vol_ratio": round(squeeze_ratio, 4),
                        "current_vol": round(current_vol, 6),
                        "historical_vol": round(historical_vol, 6),
                        "squeeze_strength": round(squeeze_strength, 4),
                        "ob_confirmed": True,
                    },
                )
        
        else:
            # Without OB, use position for direction (lower confidence)
            if position < 0.30:
                confidence = ConfidenceCalculator.calculate(
                    squeeze_strength * 0.6,
                    [],
                    max_conf=0.60,
                )
                
                return self.create_long_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Volatility squeeze (ratio={squeeze_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "vol_ratio": round(squeeze_ratio, 4),
                        "ob_confirmed": False,
                    },
                )
            
            elif position > 0.70:
                confidence = ConfidenceCalculator.calculate(
                    squeeze_strength * 0.6,
                    [],
                    max_conf=0.60,
                )
                
                return self.create_short_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Volatility squeeze (ratio={squeeze_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "vol_ratio": round(squeeze_ratio, 4),
                        "ob_confirmed": False,
                    },
                )
        
        return None


# ============================================================
# STRATEGY S6: TICK CLUSTERING
# ============================================================

class S6_TickClustering(BaseStrategy):
    """
    S6: Tick Clustering
    
    Most volume/ticks concentrated at one extreme = building S/R.
    Volume-weighted for accuracy.
    
    Logic:
    - low_cluster_ratio >= threshold AND > high_cluster_ratio → LONG
    - high_cluster_ratio >= threshold AND > low_cluster_ratio → SHORT
    
    Favorable regimes: RANGING, LOW_VOLATILITY
    """
    
    code = "S6"
    name = "S6_TickClustering"
    description = "Trade statistical tick/volume clustering"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.LOW_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 20
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        threshold = self._get_param("cluster_threshold", 0.40)
        low_ratio = metrics.low_cluster_ratio
        high_ratio = metrics.high_cluster_ratio
        
        # === LONG: Clustering at low = building support ===
        if low_ratio >= threshold and low_ratio > high_ratio:
            # Strength based on how much above threshold
            cluster_strength = min(1.0, (low_ratio - threshold) / (1 - threshold))
            
            confirmations = [
                (metrics.ob_imbalance > 0.2, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (low_ratio > 0.5, 0.05),
                (metrics.tick_flow > 0.1, 0.02),
                (metrics.has_bid_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(cluster_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Clustering at low ({low_ratio:.0%} of volume)",
                confidence=confidence,
                extra_metadata={
                    "low_cluster_ratio": round(low_ratio, 4),
                    "high_cluster_ratio": round(high_ratio, 4),
                    "threshold": threshold,
                },
            )
        
        # === SHORT: Clustering at high = building resistance ===
        if high_ratio >= threshold and high_ratio > low_ratio:
            cluster_strength = min(1.0, (high_ratio - threshold) / (1 - threshold))
            
            confirmations = [
                (metrics.ob_imbalance < -0.2, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (high_ratio > 0.5, 0.05),
                (metrics.tick_flow < -0.1, 0.02),
                (metrics.has_ask_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(cluster_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Clustering at high ({high_ratio:.0%} of volume)",
                confidence=confidence,
                extra_metadata={
                    "low_cluster_ratio": round(low_ratio, 4),
                    "high_cluster_ratio": round(high_ratio, 4),
                    "threshold": threshold,
                },
            )
        
        return None


# ============================================================
# STRATEGY S7: FAKEOUT RECOVERY
# ============================================================

class S7_FakeoutRecovery(BaseStrategy):
    """
    S7: Fakeout Recovery
    
    Price broke through band but recovered back inside.
    Classic failed breakout = strong reversal signal.
    
    Logic:
    - min_price < band.low AND last_price recovered inside → LONG
    - max_price > band.high AND last_price recovered inside → SHORT
    
    Favorable regimes: RANGING, HIGH_VOLATILITY
    """
    
    code = "S7"
    name = "S7_FakeoutRecovery"
    description = "Trade failed breakout reversals"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        recovery_threshold = self._get_param("fakeout_recovery_threshold", 0.005)
        
        # === LONG: Fakeout below low, recovered back inside ===
        if (metrics.min_price < band.low and
            metrics.last_price > band.low * (1 + recovery_threshold)):
            
            # Depth of fakeout as strength indicator
            fakeout_depth = (band.low - metrics.min_price) / band.range
            base_strength = min(1.0, fakeout_depth * 5)  # 20% depth = full strength
            
            confirmations = [
                (fakeout_depth > 0.05, 0.05),
                (fakeout_depth > 0.10, 0.05),
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.velocity_reversed and metrics.reversal_direction == "up", 0.05),
                (metrics.tick_flow_5 > 0.3, 0.05),
                (metrics.volume_ratio > 1.5, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                (metrics.has_bid_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Fakeout recovery from below (depth={fakeout_depth:.1%})",
                confidence=confidence,
                extra_metadata={
                    "fakeout_depth": round(fakeout_depth, 4),
                    "fakeout_low": round(metrics.min_price, 8),
                    "band_low": round(band.low, 8),
                    "recovery_price": round(metrics.last_price, 8),
                },
            )
        
        # === SHORT: Fakeout above high, recovered back inside ===
        if (metrics.max_price > band.high and
            metrics.last_price < band.high * (1 - recovery_threshold)):
            
            fakeout_depth = (metrics.max_price - band.high) / band.range
            base_strength = min(1.0, fakeout_depth * 5)
            
            confirmations = [
                (fakeout_depth > 0.05, 0.05),
                (fakeout_depth > 0.10, 0.05),
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.velocity_reversed and metrics.reversal_direction == "down", 0.05),
                (metrics.tick_flow_5 < -0.3, 0.05),
                (metrics.volume_ratio > 1.5, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                (metrics.has_ask_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Fakeout recovery from above (depth={fakeout_depth:.1%})",
                confidence=confidence,
                extra_metadata={
                    "fakeout_depth": round(fakeout_depth, 4),
                    "fakeout_high": round(metrics.max_price, 8),
                    "band_high": round(band.high, 8),
                    "recovery_price": round(metrics.last_price, 8),
                },
            )
        
        return None


# ============================================================
# STRATEGY S8: ORDER FLOW MOMENTUM
# ============================================================

class S8_OrderFlowMomentum(BaseStrategy):
    """
    S8: Order Flow Momentum
    
    Trade in direction of aggressive order flow.
    Combines tick classification with order book imbalance trend.
    
    Logic:
    - Combined signal = tick_flow*w1 + ob_imbalance*w2 + ob_trend*w3
    - Signal > threshold AND position < 0.6 → LONG
    - Signal < -threshold AND position > 0.4 → SHORT
    
    Favorable regimes: TRENDING_UP, TRENDING_DOWN
    """
    
    code = "S8"
    name = "S8_OrderFlowMomentum"
    description = "Trade aggressive order flow momentum"
    favorable_regimes = [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]
    uses_orderbook = True
    min_ticks_required = 20
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        # Combined signal weights
        tick_flow_weight = self._get_param("tick_flow_weight", 0.4)
        ob_imbalance_weight = self._get_param("ob_imbalance_weight", 0.4)
        ob_trend_weight = self._get_param("ob_trend_weight", 0.2)
        
        # v4.1: Include value imbalance for institutional signal
        combined = (
            metrics.tick_flow * tick_flow_weight +
            metrics.ob_depth_imbalance * (ob_imbalance_weight * 0.7) +
            metrics.ob_value_imbalance * (ob_imbalance_weight * 0.3) +
            metrics.ob_imbalance_trend * 10.0 * ob_trend_weight
        )
        
        threshold = self._get_param("order_flow_threshold", 0.30)
        position = metrics.position_in_band
        
        # === LONG: Strong bullish flow at lower part of band ===
        if combined > threshold and position < 0.6:
            base_strength = min(1.0, (combined - threshold) / 0.5)
            
            confirmations = [
                (metrics.velocity_10 > 0, 0.03),
                (metrics.tick_flow > 0.4, 0.03),
                (metrics.ob_imbalance_trend > 0.01, 0.03),
                (metrics.large_trade_buy_ratio > 0.6, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Bullish order flow (combined={combined:.2f})",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(metrics.tick_flow, 4),
                    "ob_imbalance": round(metrics.ob_depth_imbalance, 4),
                    "ob_trend": round(metrics.ob_imbalance_trend, 6),
                    "combined_signal": round(combined, 4),
                },
            )
        
        # === SHORT: Strong bearish flow at upper part of band ===
        if combined < -threshold and position > 0.4:
            base_strength = min(1.0, (abs(combined) - threshold) / 0.5)
            
            confirmations = [
                (metrics.velocity_10 < 0, 0.03),
                (metrics.tick_flow < -0.4, 0.03),
                (metrics.ob_imbalance_trend < -0.01, 0.03),
                (metrics.large_trade_buy_ratio < 0.4, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Bearish order flow (combined={combined:.2f})",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(metrics.tick_flow, 4),
                    "ob_imbalance": round(metrics.ob_depth_imbalance, 4),
                    "ob_trend": round(metrics.ob_imbalance_trend, 6),
                    "combined_signal": round(combined, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S9: WALL FADE
# ============================================================

class S9_WallFade(BaseStrategy):
    """
    S9: Wall Fade
    
    Large walls at band edges often get absorbed.
    Trade expecting the wall to hold (support/resistance).
    
    Logic:
    - Bid wall detected near band low AND price testing it → LONG
    - Ask wall detected near band high AND price testing it → SHORT
    
    Favorable regimes: RANGING
    """
    
    code = "S9"
    name = "S9_WallFade"
    description = "Trade expecting large walls to hold"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True  # Required
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        # Absolutely requires order book
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        position = metrics.position_in_band
        
        # === LONG: Large bid wall near low + price testing it ===
        if (position < StrategyConstants.LOW and metrics.has_bid_wall):
            # Check if price is actually near the wall
            if metrics.last_price < band.low * 1.01:  # Within 1% of low
                base_strength = 0.6
                
                confirmations = [
                    (metrics.tick_flow > 0.2, 0.05),  # Buying into wall
                    (metrics.ob_imbalance > 0.3, 0.05),
                    (metrics.velocity_reversed and metrics.reversal_direction == "up", 0.05),
                    (metrics.volume_ratio > 1.2, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                    (metrics.bid_wall_size > 0, 0.03),
                    (metrics.bid_depth_gradient < -0.2, 0.03),  # Deep support
                ]
                
                confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
                
                return self.create_long_signal(
                    band=band,
                    metrics=metrics,
                    reason="Bid wall absorption expected",
                    confidence=confidence,
                    extra_metadata={
                        "wall_type": "bid",
                        "wall_price": round(metrics.bid_wall_price, 8),
                        "wall_size": round(metrics.bid_wall_size, 4),
                        "price_at_wall": True,
                        "position": round(position, 4),
                    },
                )
        
        # === SHORT: Large ask wall near high + price testing it ===
        if (position > StrategyConstants.HIGH and metrics.has_ask_wall):
            if metrics.last_price > band.high * 0.99:  # Within 1% of high
                base_strength = 0.6
                
                confirmations = [
                    (metrics.tick_flow < -0.2, 0.05),
                    (metrics.ob_imbalance < -0.3, 0.05),
                    (metrics.velocity_reversed and metrics.reversal_direction == "down", 0.05),
                    (metrics.volume_ratio > 1.2, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                    (metrics.ask_wall_size > 0, 0.03),
                    (metrics.ask_depth_gradient > 0.2, 0.03),  # Deep resistance
                ]
                
                confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
                
                return self.create_short_signal(
                    band=band,
                    metrics=metrics,
                    reason="Ask wall absorption expected",
                    confidence=confidence,
                    extra_metadata={
                        "wall_type": "ask",
                        "wall_price": round(metrics.ask_wall_price, 8),
                        "wall_size": round(metrics.ask_wall_size, 4),
                        "price_at_wall": True,
                        "position": round(position, 4),
                    },
                )
        
        return None


# ============================================================
# STRATEGY S10: SPREAD NORMALIZATION
# ============================================================

class S10_SpreadNormalization(BaseStrategy):
    """
    S10: Spread Normalization
    
    Abnormally tight spreads = high liquidity = institutional activity.
    Trade mean reversion at edges when spread is tight.
    
    Logic:
    - Spread < threshold AND position < 0.20 → LONG
    - Spread < threshold AND position > 0.80 → SHORT
    
    Favorable regimes: RANGING, LOW_VOLATILITY
    """
    
    code = "S10"
    name = "S10_SpreadNormalization"
    description = "Trade when spread is abnormally tight"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.LOW_VOLATILITY]
    uses_orderbook = True  # Required
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        # Requires order book
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        spread_pct = metrics.ob_spread_pct
        threshold = self._get_param("tight_spread_threshold", 0.02)
        
        # Only trade when spread is very tight
        if spread_pct > threshold:
            return None
        
        position = metrics.position_in_band
        
        # Tighter spread = stronger signal
        spread_tightness = 1.0 - (spread_pct / threshold)
        base_strength = spread_tightness * 0.8
        
        # === LONG: At low with tight spread = institutional accumulation ===
        if position < 0.20:
            confirmations = [
                (metrics.ob_imbalance > 0.2, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow > 0, 0.02),
                (spread_pct < 0.01, 0.05),  # Very tight
                (metrics.large_trade_count > 3, 0.03),  # Institutional activity
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Tight spread at low (spread={spread_pct:.4f}%)",
                confidence=confidence,
                extra_metadata={
                    "spread_pct": round(spread_pct, 6),
                    "spread_bps": round(metrics.ob_spread_bps, 2),
                    "position": round(position, 4),
                    "spread_threshold": threshold,
                    "spread_tightness": round(spread_tightness, 4),
                },
            )
        
        # === SHORT: At high with tight spread = institutional distribution ===
        if position > 0.80:
            confirmations = [
                (metrics.ob_imbalance < -0.2, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow < 0, 0.02),
                (spread_pct < 0.01, 0.05),
                (metrics.large_trade_count > 3, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Tight spread at high (spread={spread_pct:.4f}%)",
                confidence=confidence,
                extra_metadata={
                    "spread_pct": round(spread_pct, 6),
                    "spread_bps": round(metrics.ob_spread_bps, 2),
                    "position": round(position, 4),
                    "spread_threshold": threshold,
                    "spread_tightness": round(spread_tightness, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S11: CANDLE PATTERN
# ============================================================

class S11_CandlePattern(BaseStrategy):
    """
    S11: Candle Pattern
    
    Trade based on strong candlestick patterns at band edges.
    Uses full pattern detection system.
    
    Logic:
    - Bullish pattern at band low → LONG
    - Bearish pattern at band high → SHORT
    
    Favorable regimes: All
    """
    
    code = "S11"
    name = "S11_CandlePattern"
    description = "Trade candlestick patterns at band edges"
    favorable_regimes = []  # Works in any regime
    uses_patterns = True  # This strategy requires patterns
    uses_orderbook = True
    min_ticks_required = 5
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if not self.config.use_candle_patterns:
            return None
        
        # Ensure patterns are computed
        self._compute_patterns_if_needed(metrics)
        
        if not metrics.patterns:
            return None
        
        min_strength = self._get_param("min_pattern_strength", 0.55)
        edge_threshold = self._get_param("pattern_edge_threshold", 0.25)
        position = metrics.position_in_band
        
        # === LONG: Bullish pattern at low ===
        if position < edge_threshold and metrics.strongest_bullish_pattern:
            pattern = metrics.strongest_bullish_pattern
            
            if pattern.strength < min_strength:
                return None
            
            base_strength = pattern.strength
            
            confirmations = [
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (pattern.candles_used >= 2, 0.03),  # Multi-candle = stronger
                (pattern.candles_used >= 3, 0.03),
                (metrics.tick_flow > 0.1, 0.02),
                (metrics.has_bid_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Bullish {pattern.pattern.value} at band low",
                confidence=confidence,
                extra_metadata={
                    "pattern": pattern.pattern.value,
                    "pattern_strength": round(pattern.strength, 4),
                    "pattern_direction": pattern.direction,
                    "candles_used": pattern.candles_used,
                    "position": round(position, 4),
                },
            )
        
        # === SHORT: Bearish pattern at high ===
        if position > (1 - edge_threshold) and metrics.strongest_bearish_pattern:
            pattern = metrics.strongest_bearish_pattern
            
            if pattern.strength < min_strength:
                return None
            
            base_strength = pattern.strength
            
            confirmations = [
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (pattern.candles_used >= 2, 0.03),
                (pattern.candles_used >= 3, 0.03),
                (metrics.tick_flow < -0.1, 0.02),
                (metrics.has_ask_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Bearish {pattern.pattern.value} at band high",
                confidence=confidence,
                extra_metadata={
                    "pattern": pattern.pattern.value,
                    "pattern_strength": round(pattern.strength, 4),
                    "pattern_direction": pattern.direction,
                    "candles_used": pattern.candles_used,
                    "position": round(position, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S12: STOP HUNT DETECTOR (SHEEP HUNTER)
# ============================================================

class S12_StopHunt(BaseStrategy):
    """
    S12: Stop Hunt Detector
    
    Bots push price through obvious S/R to trigger stops, then reverse.
    
    Logic:
    - Spike below band.low followed by recovery inside band → LONG
    - Spike above band.high followed by recovery inside band → SHORT
    - Dynamic threshold based on volatility (capped to avoid impossibility)
    
    Favorable regimes: HIGH_VOLATILITY
    """
    
    code = "S12"
    name = "S12_StopHunt"
    description = "Detect and trade stop hunt reversals"
    favorable_regimes = [MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 30
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        # Dynamic threshold based on volatility
        volatility = metrics.volatility_50
        if volatility <= 0:
            volatility = 0.005  # Fallback
        
        # Calculate minimum spike depth
        vol_mult = self._get_param("stop_hunt_volatility_mult", 2.0)
        min_spike_base = self._get_param("stop_hunt_min_spike", 0.002)
        max_threshold = self._get_param("stop_hunt_max_threshold", 0.03)
        
        # Use volatility-adjusted threshold, but cap it
        min_spike_raw = volatility * vol_mult
        min_spike_depth = max(min_spike_base, min(min_spike_raw, max_threshold))
        
        reversal_depth_threshold = self._get_param("stop_hunt_reversal_depth", 0.005)
        
        # === LONG: Stop hunt below band ===
        spike_below = 0.0
        if metrics.min_price < band.low:
            spike_below = (band.low - metrics.min_price) / band.low
        
        if spike_below > min_spike_depth:
            # Check for reversal back into band
            reversal_depth = (metrics.last_price - band.low) / band.range
            
            if (reversal_depth > reversal_depth_threshold and
                band.is_price_in_band(metrics.last_price)):
                
                # Strength based on spike depth
                spike_strength = min(1.0, spike_below / (min_spike_depth * 3))
                
                confirmations = [
                    (spike_below > min_spike_depth * 2, 0.05),
                    (metrics.ob_is_bullish, 0.08),
                    (metrics.tick_flow_5 > 0.3, 0.05),
                    (metrics.velocity_reversed and metrics.reversal_direction == "up", 0.05),
                    (metrics.volume_ratio > 1.5, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                    (metrics.has_bid_wall, 0.03),
                    (metrics.ob_imbalance_trend > 0.01, 0.02),
                ]
                
                confidence = ConfidenceCalculator.calculate(spike_strength, confirmations)
                
                return self.create_long_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Stop hunt detected below (spike={spike_below:.2%})",
                    confidence=confidence,
                    extra_metadata={
                        "spike_depth": round(spike_below, 6),
                        "min_spike_threshold": round(min_spike_depth, 6),
                        "reversal_depth": round(reversal_depth, 4),
                        "volatility": round(volatility, 6),
                        "hunt_type": "stop_loss_long",
                    },
                )
        
        # === SHORT: Stop hunt above band ===
        spike_above = 0.0
        if metrics.max_price > band.high:
            spike_above = (metrics.max_price - band.high) / band.high
        
        if spike_above > min_spike_depth:
            reversal_depth = (band.high - metrics.last_price) / band.range
            
            if (reversal_depth > reversal_depth_threshold and
                band.is_price_in_band(metrics.last_price)):
                
                spike_strength = min(1.0, spike_above / (min_spike_depth * 3))
                
                confirmations = [
                    (spike_above > min_spike_depth * 2, 0.05),
                    (metrics.ob_is_bearish, 0.08),
                    (metrics.tick_flow_5 < -0.3, 0.05),
                    (metrics.velocity_reversed and metrics.reversal_direction == "down", 0.05),
                    (metrics.volume_ratio > 1.5, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                    (metrics.has_ask_wall, 0.03),
                    (metrics.ob_imbalance_trend < -0.01, 0.02),
                ]
                
                confidence = ConfidenceCalculator.calculate(spike_strength, confirmations)
                
                return self.create_short_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Stop hunt detected above (spike={spike_above:.2%})",
                    confidence=confidence,
                    extra_metadata={
                        "spike_depth": round(spike_above, 6),
                        "min_spike_threshold": round(min_spike_depth, 6),
                        "reversal_depth": round(reversal_depth, 4),
                        "volatility": round(volatility, 6),
                        "hunt_type": "stop_loss_short",
                    },
                )
        
        return None


# ============================================================
# STRATEGY S13: ICEBERG DETECTOR (SHEEP HUNTER)
# ============================================================

class S13_IcebergDetector(BaseStrategy):
    """
    S13: Iceberg Order Detector
    
    Large hidden orders detected by:
    - Price stable despite heavy one-sided flow
    - Order book shows absorption
    
    Trade with the iceberg (patient money).
    
    Logic:
    - High sell flow + stable price + bullish OB → LONG (hidden buyer)
    - High buy flow + stable price + bearish OB → SHORT (hidden seller)
    
    Favorable regimes: RANGING
    """
    
    code = "S13"
    name = "S13_IcebergDetector"
    description = "Detect hidden iceberg orders"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True
    min_ticks_required = 40
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        stability_threshold = self._get_param("iceberg_stability_threshold", 0.001)
        flow_threshold = self._get_param("iceberg_flow_threshold", 0.40)
        
        # Calculate price stability from range vs mean
        if metrics.last_price <= 0:
            return None
        
        price_range_pct = metrics.price_range / metrics.last_price
        
        # Price must be stable
        if price_range_pct > stability_threshold:
            return None
        
        # Need significant one-sided flow
        if abs(metrics.tick_flow) < flow_threshold:
            return None
        
        position = metrics.position_in_band
        
        # === LONG: Heavy selling but price stable = iceberg bid absorbing ===
        if metrics.tick_flow < -flow_threshold and position < 0.4:
            # Key signal: OB shows bid strength DESPITE selling pressure
            contradiction = metrics.ob_imbalance > 0.1  # Buyers in book
            
            base_strength = 0.65 if contradiction else 0.55
            
            confirmations = [
                (contradiction, 0.08),  # The contradiction IS the signal
                (metrics.has_bid_wall, 0.05),
                (abs(metrics.tick_flow) > 0.5, 0.03),
                (price_range_pct < 0.0005, 0.03),  # Very stable
                (metrics.ob_imbalance_trend > 0, 0.02),
                (metrics.large_trade_count > 5, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Iceberg bid detected (flow={metrics.tick_flow:.2f}, stable)",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(metrics.tick_flow, 4),
                    "price_stability": round(price_range_pct, 6),
                    "ob_contradiction": contradiction,
                    "ob_imbalance": round(metrics.ob_imbalance, 4),
                    "iceberg_side": "bid",
                    "position": round(position, 4),
                },
            )
        
        # === SHORT: Heavy buying but price stable = iceberg ask absorbing ===
        if metrics.tick_flow > flow_threshold and position > 0.6:
            contradiction = metrics.ob_imbalance < -0.1  # Sellers in book
            
            base_strength = 0.65 if contradiction else 0.55
            
            confirmations = [
                (contradiction, 0.08),
                (metrics.has_ask_wall, 0.05),
                (abs(metrics.tick_flow) > 0.5, 0.03),
                (price_range_pct < 0.0005, 0.03),
                (metrics.ob_imbalance_trend < 0, 0.02),
                (metrics.large_trade_count > 5, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Iceberg ask detected (flow={metrics.tick_flow:.2f}, stable)",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(metrics.tick_flow, 4),
                    "price_stability": round(price_range_pct, 6),
                    "ob_contradiction": contradiction,
                    "ob_imbalance": round(metrics.ob_imbalance, 4),
                    "iceberg_side": "ask",
                    "position": round(position, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S14: LIQUIDITY SWEEP (SHEEP HUNTER)
# ============================================================

class S14_LiquiditySweep(BaseStrategy):
    """
    S14: Liquidity Sweep
    
    Rapid move through multiple levels, grabbing liquidity, then reversal.
    Different from stop hunt - this is intentional liquidity grab.
    
    Logic:
    - Price swept low zone then recovered → LONG
    - Price swept high zone then recovered → SHORT
    
    Favorable regimes: HIGH_VOLATILITY
    """
    
    code = "S14"
    name = "S14_LiquiditySweep"
    description = "Detect liquidity sweeps and trade reversal"
    favorable_regimes = [MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 25
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        min_range = self._get_param("sweep_min_range", 0.15)
        reversal_confirm = self._get_param("sweep_reversal_confirm", 0.05)
        
        # Check if price range was large enough
        sweep_range = metrics.price_range / band.range
        
        if sweep_range < min_range:
            return None
        
        position = metrics.position_in_band
        
        # Position of extremes in band
        low_position = band.position_in_band(metrics.min_price)
        high_position = band.position_in_band(metrics.max_price)
        
        # === LONG: Down sweep then recovery ===
        if low_position < 0.20 and position > low_position + reversal_confirm:
            # Strength based on sweep size
            base_strength = min(1.0, sweep_range / 0.3)  # 30% = full strength
            
            confirmations = [
                (sweep_range > 0.25, 0.05),
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow_5 > 0.4, 0.05),
                (metrics.velocity_10 > 0.01, 0.03),
                (metrics.velocity_reversed and metrics.reversal_direction == "up", 0.04),
                (metrics.has_bid_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Liquidity sweep down reversed (range={sweep_range:.1%})",
                confidence=confidence,
                extra_metadata={
                    "sweep_range": round(sweep_range, 4),
                    "sweep_low": round(metrics.min_price, 8),
                    "sweep_low_position": round(low_position, 4),
                    "current_position": round(position, 4),
                    "sweep_direction": "down",
                },
            )
        
        # === SHORT: Up sweep then recovery ===
        if high_position > 0.80 and position < high_position - reversal_confirm:
            base_strength = min(1.0, sweep_range / 0.3)
            
            confirmations = [
                (sweep_range > 0.25, 0.05),
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow_5 < -0.4, 0.05),
                (metrics.velocity_10 < -0.01, 0.03),
                (metrics.velocity_reversed and metrics.reversal_direction == "down", 0.04),
                (metrics.has_ask_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Liquidity sweep up reversed (range={sweep_range:.1%})",
                confidence=confidence,
                extra_metadata={
                    "sweep_range": round(sweep_range, 4),
                    "sweep_high": round(metrics.max_price, 8),
                    "sweep_high_position": round(high_position, 4),
                    "current_position": round(position, 4),
                    "sweep_direction": "up",
                },
            )
        
        return None


# ============================================================
# STRATEGY S15: BOT EXHAUSTION (SHEEP HUNTER)
# ============================================================

class S15_BotExhaustion(BaseStrategy):
    """
    S15: Bot Exhaustion
    
    After sustained one-directional flow, bots run out of ammunition.
    Detected by decreasing velocity.
    
    Logic:
    - At extreme high + bullish momentum dying → SHORT
    - At extreme low + bearish momentum dying → LONG
    
    Favorable regimes: HIGH_VOLATILITY, TRENDING_UP, TRENDING_DOWN
    """
    
    code = "S15"
    name = "S15_BotExhaustion"
    description = "Detect exhausted momentum bots"
    favorable_regimes = [
        MarketRegime.HIGH_VOLATILITY,
        MarketRegime.TRENDING_UP,
        MarketRegime.TRENDING_DOWN,
    ]
    uses_orderbook = True
    min_ticks_required = 35
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        decay_threshold = self._get_param("exhaustion_velocity_decay", 0.5)
        
        # Only at extremes
        if (StrategyConstants.EXTREME_LOW < metrics.position_in_band <
            StrategyConstants.EXTREME_HIGH):
            return None
        
        # Compare old vs new velocity
        old_velocity = metrics.velocity_30
        recent_velocity = metrics.velocity_5
        
        if abs(old_velocity) < 0.001:
            return None
        
        velocity_ratio = abs(recent_velocity) / abs(old_velocity)
        velocity_decaying = velocity_ratio < decay_threshold
        
        if not velocity_decaying:
            return None
        
        # === SHORT: Bullish exhaustion at high ===
        if metrics.position_in_band > StrategyConstants.EXTREME_HIGH:
            if old_velocity > 0:  # Was moving up
                decay_strength = 1.0 - velocity_ratio
                
                confirmations = [
                    (velocity_ratio < 0.3, 0.05),  # Very decayed
                    (metrics.ob_imbalance < 0, 0.05),
                    (metrics.ob_spread_pct > 0.05, 0.03),  # Spread widening
                    (metrics.tick_flow_5 < 0, 0.03),
                    (metrics.ob_imbalance_trend < 0, 0.02),
                ]
                
                confidence = ConfidenceCalculator.calculate(decay_strength, confirmations)
                
                return self.create_short_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Bot exhaustion at high (vel_decay={velocity_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "velocity_ratio": round(velocity_ratio, 4),
                        "old_velocity": round(old_velocity, 6),
                        "recent_velocity": round(recent_velocity, 6),
                        "exhaustion_type": "bullish_exhaustion",
                        "position": round(metrics.position_in_band, 4),
                    },
                )
        
        # === LONG: Bearish exhaustion at low ===
        if metrics.position_in_band < StrategyConstants.EXTREME_LOW:
            if old_velocity < 0:  # Was moving down
                decay_strength = 1.0 - velocity_ratio
                
                confirmations = [
                    (velocity_ratio < 0.3, 0.05),
                    (metrics.ob_imbalance > 0, 0.05),
                    (metrics.ob_spread_pct > 0.05, 0.03),
                    (metrics.tick_flow_5 > 0, 0.03),
                    (metrics.ob_imbalance_trend > 0, 0.02),
                ]
                
                confidence = ConfidenceCalculator.calculate(decay_strength, confirmations)
                
                return self.create_long_signal(
                    band=band,
                    metrics=metrics,
                    reason=f"Bot exhaustion at low (vel_decay={velocity_ratio:.2f})",
                    confidence=confidence,
                    extra_metadata={
                        "velocity_ratio": round(velocity_ratio, 4),
                        "old_velocity": round(old_velocity, 6),
                        "recent_velocity": round(recent_velocity, 6),
                        "exhaustion_type": "bearish_exhaustion",
                        "position": round(metrics.position_in_band, 4),
                    },
                )
        
        return None


# ============================================================
# STRATEGY S16: IMBALANCE DIVERGENCE (SHEEP HUNTER)
# ============================================================

class S16_ImbalanceDivergence(BaseStrategy):
    """
    S16: Imbalance Divergence
    
    When tick flow diverges from order book imbalance, trade with OB.
    The order book shows where the real liquidity is.
    
    Logic (FIXED):
    - Tick flow bullish + OB bearish → SHORT (buyers will exhaust)
    - Tick flow bearish + OB bullish → LONG (sellers will exhaust)
    
    Both signals must be STRONG and in OPPOSITE directions.
    
    Favorable regimes: RANGING
    """
    
    code = "S16"
    name = "S16_ImbalanceDivergence"
    description = "Trade when tick flow diverges from order book"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True  # Required
    min_ticks_required = 25
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        tick_flow = metrics.tick_flow
        ob_imbalance = metrics.ob_depth_imbalance
        threshold = self._get_param("divergence_threshold", 0.35)
        
        # FIXED: Check for STRONG divergence - opposite signs AND both strong
        bullish_tick_bearish_ob = (
            tick_flow > threshold and
            ob_imbalance < -threshold
        )
        
        bearish_tick_bullish_ob = (
            tick_flow < -threshold and
            ob_imbalance > threshold
        )
        
        if not (bullish_tick_bearish_ob or bearish_tick_bullish_ob):
            return None
        
        position = metrics.position_in_band
        
        # === SHORT: Tick flow bullish, OB bearish = buyers will exhaust ===
        if bullish_tick_bearish_ob and position > 0.4:
            divergence_strength = min(1.0, (tick_flow - ob_imbalance) / 1.5)
            
            confirmations = [
                (abs(tick_flow) > 0.5, 0.03),
                (abs(ob_imbalance) > 0.5, 0.03),
                (metrics.ob_imbalance_trend < 0, 0.05),
                (position > 0.5, 0.02),
                (metrics.has_ask_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(divergence_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Divergence: tick bull ({tick_flow:.2f}) vs OB bear ({ob_imbalance:.2f})",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(tick_flow, 4),
                    "ob_imbalance": round(ob_imbalance, 4),
                    "divergence_type": "bull_tick_bear_ob",
                    "divergence_strength": round(divergence_strength, 4),
                },
            )
        
        # === LONG: Tick flow bearish, OB bullish = sellers will exhaust ===
        if bearish_tick_bullish_ob and position < 0.6:
            divergence_strength = min(1.0, (ob_imbalance - tick_flow) / 1.5)
            
            confirmations = [
                (abs(tick_flow) > 0.5, 0.03),
                (abs(ob_imbalance) > 0.5, 0.03),
                (metrics.ob_imbalance_trend > 0, 0.05),
                (position < 0.5, 0.02),
                (metrics.has_bid_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(divergence_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Divergence: tick bear ({tick_flow:.2f}) vs OB bull ({ob_imbalance:.2f})",
                confidence=confidence,
                extra_metadata={
                    "tick_flow": round(tick_flow, 4),
                    "ob_imbalance": round(ob_imbalance, 4),
                    "divergence_type": "bear_tick_bull_ob",
                    "divergence_strength": round(divergence_strength, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S17: MOMENTUM TRAP (SHEEP HUNTER)
# ============================================================

class S17_MomentumTrap(BaseStrategy):
    """
    S17: Momentum Trap
    
    Strong momentum suddenly dies at extreme = trapped chasers.
    Trade the reversal as trapped traders exit.
    
    Logic:
    - At extreme high + strong prior momentum + current momentum dead → SHORT
    - At extreme low + strong prior momentum + current momentum dead → LONG
    
    Favorable regimes: HIGH_VOLATILITY
    """
    
    code = "S17"
    name = "S17_MomentumTrap"
    description = "Fade trapped momentum chasers"
    favorable_regimes = [MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 35
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        strong_threshold = self._get_param("momentum_strong_threshold", 0.015)
        death_threshold = self._get_param("momentum_death_threshold", 0.003)
        
        position = metrics.position_in_band
        
        # Only at extremes
        if (StrategyConstants.EXTREME_LOW < position <
            StrategyConstants.EXTREME_HIGH):
            return None
        
        old_momentum = metrics.velocity_20
        recent_momentum = metrics.velocity_5
        
        # === SHORT: Bullish momentum dying at high ===
        if position > StrategyConstants.EXTREME_HIGH:
            if old_momentum > strong_threshold:
                if abs(recent_momentum) < death_threshold:
                    trap_strength = 0.7
                    
                    confirmations = [
                        (
                            metrics.velocity_reversed and
                            metrics.reversal_direction == "down",
                            0.08
                        ),
                        (metrics.ob_imbalance < 0, 0.05),
                        (metrics.tick_flow_5 < 0, 0.05),
                        (old_momentum > 0.02, 0.03),
                        (metrics.has_ask_wall, 0.02),
                    ]
                    
                    confidence = ConfidenceCalculator.calculate(
                        trap_strength,
                        confirmations
                    )
                    
                    return self.create_short_signal(
                        band=band,
                        metrics=metrics,
                        reason=f"Momentum trap at high (old={old_momentum:.4f}, now={recent_momentum:.4f})",
                        confidence=confidence,
                        extra_metadata={
                            "old_momentum": round(old_momentum, 6),
                            "recent_momentum": round(recent_momentum, 6),
                            "trap_type": "bull_trap",
                            "position": round(position, 4),
                        },
                    )
        
        # === LONG: Bearish momentum dying at low ===
        if position < StrategyConstants.EXTREME_LOW:
            if old_momentum < -strong_threshold:
                if abs(recent_momentum) < death_threshold:
                    trap_strength = 0.7
                    
                    confirmations = [
                        (
                            metrics.velocity_reversed and
                            metrics.reversal_direction == "up",
                            0.08
                        ),
                        (metrics.ob_imbalance > 0, 0.05),
                        (metrics.tick_flow_5 > 0, 0.05),
                        (old_momentum < -0.02, 0.03),
                        (metrics.has_bid_wall, 0.02),
                    ]
                    
                    confidence = ConfidenceCalculator.calculate(
                        trap_strength,
                        confirmations
                    )
                    
                    return self.create_long_signal(
                        band=band,
                        metrics=metrics,
                        reason=f"Momentum trap at low (old={old_momentum:.4f}, now={recent_momentum:.4f})",
                        confidence=confidence,
                        extra_metadata={
                            "old_momentum": round(old_momentum, 6),
                            "recent_momentum": round(recent_momentum, 6),
                            "trap_type": "bear_trap",
                            "position": round(position, 4),
                        },
                    )
        
        return None


# ============================================================
# STRATEGY S18: VWAP DEVIATION
# ============================================================

class S18_VWAPDeviation(BaseStrategy):
    """
    S18: VWAP Deviation
    
    Price deviates significantly from VWAP = mean reversion opportunity.
    Institutional traders often revert to VWAP.
    
    Logic:
    - Price above VWAP by threshold → SHORT
    - Price below VWAP by threshold → LONG
    
    Favorable regimes: RANGING
    """
    
    code = "S18"
    name = "S18_VWAPDeviation"
    description = "Trade deviations from VWAP"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True
    min_ticks_required = 20
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        if metrics.vwap <= 0:
            return None
        
        deviation_threshold = self._get_param("vwap_deviation_threshold", 0.005)
        
        # === SHORT: Price above VWAP ===
        if metrics.vwap_deviation > deviation_threshold:
            base_strength = min(
                1.0,
                (metrics.vwap_deviation - deviation_threshold) / 0.01
            )
            
            confirmations = [
                (metrics.ob_is_bearish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.position_in_band > 0.6, 0.03),
                (metrics.tick_flow < 0, 0.02),
                (metrics.ob_imbalance_trend < 0, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            # Target: revert to VWAP
            exit_price = metrics.vwap
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"VWAP deviation high ({metrics.vwap_deviation:.2%})",
                confidence=confidence,
                exit_price=exit_price,
                extra_metadata={
                    "vwap_deviation": round(metrics.vwap_deviation, 6),
                    "vwap": round(metrics.vwap, 8),
                    "last_price": round(metrics.last_price, 8),
                },
            )
        
        # === LONG: Price below VWAP ===
        if metrics.vwap_deviation < -deviation_threshold:
            base_strength = min(
                1.0,
                (abs(metrics.vwap_deviation) - deviation_threshold) / 0.01
            )
            
            confirmations = [
                (metrics.ob_is_bullish, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.position_in_band < 0.4, 0.03),
                (metrics.tick_flow > 0, 0.02),
                (metrics.ob_imbalance_trend > 0, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            exit_price = metrics.vwap
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"VWAP deviation low ({metrics.vwap_deviation:.2%})",
                confidence=confidence,
                exit_price=exit_price,
                extra_metadata={
                    "vwap_deviation": round(metrics.vwap_deviation, 6),
                    "vwap": round(metrics.vwap, 8),
                    "last_price": round(metrics.last_price, 8),
                },
            )
        
        return None


# ============================================================
# STRATEGY S19: ABSORPTION DETECTOR
# ============================================================

class S19_AbsorptionDetector(BaseStrategy):
    """
    S19: Absorption Detector
    
    Large volume at extreme but price doesn't move = absorption.
    Indicates institutional buying/selling.
    
    Logic:
    - High volume + stable price + bullish flow/OB at low → LONG (accumulation)
    - High volume + stable price + bearish flow/OB at high → SHORT (distribution)
    
    Favorable regimes: RANGING, HIGH_VOLATILITY
    """
    
    code = "S19"
    name = "S19_AbsorptionDetector"
    description = "Detect large volume absorption"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 30
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        if metrics.total_volume <= 0:
            return None
        
        volume_ratio = self._get_param("absorption_volume_ratio", 2.5)
        stability_threshold = self._get_param("absorption_price_stability", 0.0008)
        
        # Calculate volume relative to average per tick
        avg_volume_per_tick = metrics.avg_tick_size
        high_volume = metrics.total_volume > avg_volume_per_tick * metrics.tick_count * volume_ratio
        
        if not high_volume:
            return None
        
        # Check price stability despite high volume
        price_range_pct = (
            metrics.price_range / metrics.last_price
            if metrics.last_price > 0 else 0
        )
        
        if price_range_pct > stability_threshold:
            return None  # Too much movement
        
        # Determine direction from flow and OB
        position = metrics.position_in_band
        
        # === LONG: Absorption at low = accumulation ===
        if (metrics.tick_flow > 0.3 and
            metrics.ob_imbalance > 0.2 and
            position < 0.4):
            
            base_strength = 0.65
            
            confirmations = [
                (metrics.large_trade_count > 5, 0.05),
                (metrics.ob_imbalance_trend > 0, 0.03),
                (metrics.has_bid_wall, 0.03),
                (price_range_pct < 0.0005, 0.03),  # Very stable
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            vol_multiple = metrics.total_volume / (avg_volume_per_tick * metrics.tick_count)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Absorption at low (vol={vol_multiple:.1f}x, stable)",
                confidence=confidence,
                extra_metadata={
                    "absorption_type": "accumulation",
                    "volume_multiple": round(vol_multiple, 2),
                    "price_stability": round(price_range_pct, 6),
                    "large_trade_count": metrics.large_trade_count,
                },
            )
        
        # === SHORT: Absorption at high = distribution ===
        if (metrics.tick_flow < -0.3 and
            metrics.ob_imbalance < -0.2 and
            position > 0.6):
            
            base_strength = 0.65
            
            confirmations = [
                (metrics.large_trade_count > 5, 0.05),
                (metrics.ob_imbalance_trend < 0, 0.03),
                (metrics.has_ask_wall, 0.03),
                (price_range_pct < 0.0005, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            vol_multiple = metrics.total_volume / (avg_volume_per_tick * metrics.tick_count)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Absorption at high (vol={vol_multiple:.1f}x, stable)",
                confidence=confidence,
                extra_metadata={
                    "absorption_type": "distribution",
                    "volume_multiple": round(vol_multiple, 2),
                    "price_stability": round(price_range_pct, 6),
                    "large_trade_count": metrics.large_trade_count,
                },
            )
        
        return None


# ============================================================
# STRATEGY S20: DEPTH GRADIENT
# ============================================================

class S20_DepthGradient(BaseStrategy):
    """
    S20: Depth Gradient
    
    Analyze how order book depth changes across levels.
    Negative gradient (thin at top, thick below) = support building.
    Positive gradient (thick at top, thin below) = resistance building.
    
    Logic:
    - Negative bid gradient at low → LONG (deep support)
    - Positive ask gradient at high → SHORT (deep resistance)
    
    Favorable regimes: RANGING
    """
    
    code = "S20"
    name = "S20_DepthGradient"
    description = "Trade based on order book depth gradient"
    favorable_regimes = [MarketRegime.RANGING]
    uses_orderbook = True  # Required
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        threshold = self._get_param("depth_gradient_threshold", 0.30)
        position = metrics.position_in_band
        
        # === LONG: Negative bid gradient = support building at depth ===
        if (metrics.bid_depth_gradient < -threshold and position < 0.4):
            base_strength = min(1.0, abs(metrics.bid_depth_gradient))
            
            confirmations = [
                (metrics.ob_imbalance > 0.1, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow > 0, 0.02),
                (metrics.bid_depth_gradient < -0.5, 0.05),  # Strong gradient
                (metrics.has_bid_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Bid depth gradient ({metrics.bid_depth_gradient:.2f}) shows support",
                confidence=confidence,
                extra_metadata={
                    "bid_depth_gradient": round(metrics.bid_depth_gradient, 4),
                    "ask_depth_gradient": round(metrics.ask_depth_gradient, 4),
                    "threshold": threshold,
                    "position": round(position, 4),
                },
            )
        
        # === SHORT: Positive ask gradient = resistance building at depth ===
        if (metrics.ask_depth_gradient > threshold and position > 0.6):
            base_strength = min(1.0, metrics.ask_depth_gradient)
            
            confirmations = [
                (metrics.ob_imbalance < -0.1, self._get_param(
                    "ob_confirmation_boost",
                    StrategyConstants.OB_CONFIRMATION_BOOST
                )),
                (metrics.tick_flow < 0, 0.02),
                (metrics.ask_depth_gradient > 0.5, 0.05),
                (metrics.has_ask_wall, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Ask depth gradient ({metrics.ask_depth_gradient:.2f}) shows resistance",
                confidence=confidence,
                extra_metadata={
                    "bid_depth_gradient": round(metrics.bid_depth_gradient, 4),
                    "ask_depth_gradient": round(metrics.ask_depth_gradient, 4),
                    "threshold": threshold,
                    "position": round(position, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S21: QUEUE IMBALANCE (FIXED)
# ============================================================

class S21_QueueImbalance(BaseStrategy):
    """
    S21: Queue Imbalance
    
    Compare top-of-book imbalance vs deep imbalance.
    If both agree and are strong = immediate pressure.
    
    Logic (FIXED - uses actual top vs deep imbalance):
    - Top imbalance bullish AND deep imbalance bullish AND position < 0.5 → LONG
    - Top imbalance bearish AND deep imbalance bearish AND position > 0.5 → SHORT
    
    Favorable regimes: ALL
    """
    
    code = "S21"
    name = "S21_QueueImbalance"
    description = "Trade immediate queue pressure"
    favorable_regimes = []  # Works in all regimes
    uses_orderbook = True  # Required
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        threshold = self._get_param("queue_imbalance_threshold", 0.40)
        
        # Get top-of-book and deep imbalance
        top_imbalance = metrics.ob_imbalance  # Top of book
        deep_imbalance = metrics.ob_depth_imbalance  # 10 levels
        
        # Both must be significant
        if abs(top_imbalance) < threshold or abs(deep_imbalance) < threshold * 0.5:
            return None
        
        # Check if they agree (same sign)
        same_direction = top_imbalance * deep_imbalance > 0
        
        if not same_direction:
            return None  # Conflicting signals
        
        position = metrics.position_in_band
        
        # Calculate agreement strength
        agreement_strength = min(abs(top_imbalance), abs(deep_imbalance))
        
        # === LONG: Strong bullish queue pressure ===
        if top_imbalance > threshold and deep_imbalance > 0 and position < 0.5:
            base_strength = min(1.0, agreement_strength * 1.5)
            
            confirmations = [
                (metrics.tick_flow > 0.2, 0.03),
                (metrics.ob_imbalance_trend > 0, 0.03),
                (deep_imbalance > threshold, 0.03),  # Deep also strong
                (metrics.has_bid_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Queue imbalance bullish (top={top_imbalance:.2f}, deep={deep_imbalance:.2f})",
                confidence=confidence,
                extra_metadata={
                    "top_imbalance": round(top_imbalance, 4),
                    "deep_imbalance": round(deep_imbalance, 4),
                    "agreement_strength": round(agreement_strength, 4),
                    "position": round(position, 4),
                },
            )
        
        # === SHORT: Strong bearish queue pressure ===
        if top_imbalance < -threshold and deep_imbalance < 0 and position > 0.5:
            base_strength = min(1.0, agreement_strength * 1.5)
            
            confirmations = [
                (metrics.tick_flow < -0.2, 0.03),
                (metrics.ob_imbalance_trend < 0, 0.03),
                (deep_imbalance < -threshold, 0.03),
                (metrics.has_ask_wall, 0.02),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Queue imbalance bearish (top={top_imbalance:.2f}, deep={deep_imbalance:.2f})",
                confidence=confidence,
                extra_metadata={
                    "top_imbalance": round(top_imbalance, 4),
                    "deep_imbalance": round(deep_imbalance, 4),
                    "agreement_strength": round(agreement_strength, 4),
                    "position": round(position, 4),
                },
            )
        
        return None


# ============================================================
# STRATEGY S22: INSTITUTIONAL FLOW
# ============================================================

class S22_InstitutionalFlow(BaseStrategy):
    """
    S22: Institutional Flow
    
    Detect institutional activity through:
    - Large trade clustering
    - Sustained order book imbalance
    - Low velocity (patient accumulation)
    
    Logic:
    - Large buy trades at low + bullish OB trend + low velocity → LONG
    - Large sell trades at high + bearish OB trend + low velocity → SHORT
    
    Favorable regimes: RANGING, LOW_VOLATILITY
    """
    
    code = "S22"
    name = "S22_InstitutionalFlow"
    description = "Detect institutional accumulation/distribution"
    favorable_regimes = [MarketRegime.RANGING, MarketRegime.LOW_VOLATILITY]
    uses_orderbook = True
    min_ticks_required = 40
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if metrics.tick_count < self.min_ticks_required:
            return None
        
        min_large_trades = int(self._get_param("institutional_cluster_count", 4))
        
        # Check for large trade clustering
        if metrics.large_trade_count < min_large_trades:
            return None
        
        # Check sustained OB imbalance
        if abs(metrics.ob_imbalance_trend) < 0.005:
            return None
        
        # Check low velocity (patient, not aggressive)
        if abs(metrics.velocity_10) > 0.01:
            return None
        
        position = metrics.position_in_band
        
        # === LONG: Accumulation at low ===
        if (metrics.large_trade_buy_ratio > 0.6 and
            position < 0.4 and
            metrics.ob_imbalance_trend > 0):
            
            base_strength = min(1.0, metrics.large_trade_count / (min_large_trades * 2))
            
            confirmations = [
                (metrics.large_trade_buy_ratio > 0.7, 0.05),
                (metrics.ob_imbalance > 0.2, 0.05),
                (metrics.volume_ratio > 1.3, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                (metrics.has_bid_wall, 0.03),
                (metrics.large_trade_count > min_large_trades * 1.5, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"Institutional accumulation ({metrics.large_trade_count} large trades)",
                confidence=confidence,
                extra_metadata={
                    "large_trade_count": metrics.large_trade_count,
                    "large_trade_buy_count": metrics.large_trade_buy_count,
                    "large_trade_sell_count": metrics.large_trade_sell_count,
                    "large_trade_buy_ratio": round(metrics.large_trade_buy_ratio, 4),
                    "ob_imbalance_trend": round(metrics.ob_imbalance_trend, 6),
                    "flow_type": "accumulation",
                },
            )
        
        # === SHORT: Distribution at high ===
        if (metrics.large_trade_buy_ratio < 0.4 and
            position > 0.6 and
            metrics.ob_imbalance_trend < 0):
            
            base_strength = min(1.0, metrics.large_trade_count / (min_large_trades * 2))
            
            confirmations = [
                (metrics.large_trade_buy_ratio < 0.3, 0.05),
                (metrics.ob_imbalance < -0.2, 0.05),
                (metrics.volume_ratio > 1.3, StrategyConstants.VOLUME_CONFIRMATION_BOOST),
                (metrics.has_ask_wall, 0.03),
                (metrics.large_trade_count > min_large_trades * 1.5, 0.03),
            ]
            
            confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
            
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"Institutional distribution ({metrics.large_trade_count} large trades)",
                confidence=confidence,
                extra_metadata={
                    "large_trade_count": metrics.large_trade_count,
                    "large_trade_buy_count": metrics.large_trade_buy_count,
                    "large_trade_sell_count": metrics.large_trade_sell_count,
                    "large_trade_buy_ratio": round(metrics.large_trade_buy_ratio, 4),
                    "ob_imbalance_trend": round(metrics.ob_imbalance_trend, 6),
                    "flow_type": "distribution",
                },
            )
        
        return None


# ============================================================
# STRATEGY S23: MARKET MAKING EDGE (FIXED)
# ============================================================

class S23_MarketMakingEdge(BaseStrategy):
    """
    S23: Market Making Edge
    
    Capture spread when conditions are favorable:
    - Wide spread
    - Low volatility
    - Balanced order book
    
    Logic (FIXED - validates edge exists):
    - Spread > threshold AND low vol AND balanced OB → Trade to microprice
    - Direction based on microprice deviation from last price
    
    Favorable regimes: LOW_VOLATILITY, RANGING
    """
    
    code = "S23"
    name = "S23_MarketMakingEdge"
    description = "Capture spread in favorable conditions"
    favorable_regimes = [MarketRegime.LOW_VOLATILITY, MarketRegime.RANGING]
    uses_orderbook = True  # Required
    min_ticks_required = 10
    
    def evaluate(
        self,
        band: BandData,
        metrics: PrecomputedMetrics,
    ) -> Optional[TradeSignal]:
        
        if not metrics.ob_available or metrics.ob_stale:
            return None
        
        min_spread_bps = self._get_param("mm_min_spread_bps", 6.0)
        imbalance_limit = self._get_param("mm_imbalance_threshold", 0.10)
        
        # Need wide spread
        if metrics.ob_spread_bps < min_spread_bps:
            return None
        
        # Need low volatility
        if metrics.volatility_20 > 0.01:
            return None
        
        # Need balanced order book (not one-sided)
        if abs(metrics.ob_imbalance) > imbalance_limit:
            return None
        
        position = metrics.position_in_band
        
        # Only trade at midpoint (market making)
        if not (0.4 < position < 0.6):
            return None
        
        # Calculate edge based on microprice vs weighted mid
        fair_price = (metrics.ob_microprice + metrics.ob_weighted_mid) / 2
        
        if fair_price <= 0 or metrics.last_price <= 0:
            return None
        
        # Calculate expected edge
        price_diff = fair_price - metrics.last_price
        edge_bps = abs(price_diff) / metrics.last_price * 10000
        
        # Validate edge covers transaction costs
        min_edge_bps = self.config.total_cost * 10000
        if edge_bps < min_edge_bps:
            return None
        
        base_strength = min(1.0, (metrics.ob_spread_bps - min_spread_bps) / 10.0)
        
        confirmations = [
            (metrics.ob_spread_bps > min_spread_bps + 2, 0.05),
            (metrics.volatility_20 < 0.005, 0.03),
            (abs(metrics.ob_imbalance) < 0.05, 0.03),
            (edge_bps > min_edge_bps * 1.5, 0.03),
        ]
        
        confidence = ConfidenceCalculator.calculate(base_strength, confirmations)
        
        # Direction based on microprice deviation
        if price_diff > 0:
            # Fair price above current = LONG
            return self.create_long_signal(
                band=band,
                metrics=metrics,
                reason=f"MM edge: spread={metrics.ob_spread_bps:.1f}bps, edge={edge_bps:.1f}bps",
                confidence=confidence,
                entry_price=metrics.last_price,
                exit_price=fair_price,
                extra_metadata={
                    "spread_bps": round(metrics.ob_spread_bps, 2),
                    "edge_bps": round(edge_bps, 2),
                    "fair_price": round(fair_price, 8),
                    "microprice": round(metrics.ob_microprice, 8),
                    "weighted_mid": round(metrics.ob_weighted_mid, 8),
                    "volatility": round(metrics.volatility_20, 6),
                    "imbalance": round(metrics.ob_imbalance, 4),
                    "mm_type": "long_edge",
                },
            )
        else:
            # Fair price below current = SHORT
            return self.create_short_signal(
                band=band,
                metrics=metrics,
                reason=f"MM edge: spread={metrics.ob_spread_bps:.1f}bps, edge={edge_bps:.1f}bps",
                confidence=confidence,
                entry_price=metrics.last_price,
                exit_price=fair_price,
                extra_metadata={
                    "spread_bps": round(metrics.ob_spread_bps, 2),
                    "edge_bps": round(edge_bps, 2),
                    "fair_price": round(fair_price, 8),
                    "microprice": round(metrics.ob_microprice, 8),
                    "weighted_mid": round(metrics.ob_weighted_mid, 8),
                    "volatility": round(metrics.volatility_20, 6),
                    "imbalance": round(metrics.ob_imbalance, 4),
                    "mm_type": "short_edge",
                },
            )

# ============================================================
# STRATEGY REGISTRY
# ============================================================

STRATEGY_CLASSES: List[type] = [
    S1_PathClear,
    S2_MeanReversion,
    S3_MultiTouch,
    S4_SimpleBandEntry,
    S5_VolatilitySqueeze,
    S6_TickClustering,
    S7_FakeoutRecovery,
    S8_OrderFlowMomentum,
    S9_WallFade,
    S10_SpreadNormalization,
    S11_CandlePattern,
    S12_StopHunt,
    S13_IcebergDetector,
    S14_LiquiditySweep,
    S15_BotExhaustion,
    S16_ImbalanceDivergence,
    S17_MomentumTrap,
    S18_VWAPDeviation,
    S19_AbsorptionDetector,
    S20_DepthGradient,
    S21_QueueImbalance,
    S22_InstitutionalFlow,
    S23_MarketMakingEdge,
]

ALL_STRATEGY_CODES: List[str] = [cls.code for cls in STRATEGY_CLASSES]
STRATEGY_MAP: Dict[str, type] = {cls.code: cls for cls in STRATEGY_CLASSES}


def get_strategy_info() -> List[Dict[str, str]]:
    """Get metadata for all registered strategies."""
    return [
        {
            "code": cls.code,
            "name": cls.name,
            "description": cls.description,
            "favorable_regimes": [r.value for r in cls.favorable_regimes] if cls.favorable_regimes else ["all"],
        }
        for cls in STRATEGY_CLASSES
    ]


def parse_strategy_filter(filter_str: str) -> List[str]:
    """
    Parse strategy filter string.
    
    Examples:
        "all" -> all strategies
        "S1,S2,S3" -> specific strategies
        "S1-S5" -> range
        "S1-S5,S12,S15-S17" -> mixed
    """
    if not filter_str or filter_str.lower() == "all":
        return ALL_STRATEGY_CODES
    
    result: List[str] = []
    parts = [p.strip().upper() for p in filter_str.split(",") if p.strip()]
    
    for part in parts:
        if "-" in part and part.count("-") == 1:
            try:
                start, end = part.split("-")
                start_num = int(start.replace("S", ""))
                end_num = int(end.replace("S", ""))
                
                for i in range(start_num, end_num + 1):
                    code = f"S{i}"
                    if code in ALL_STRATEGY_CODES and code not in result:
                        result.append(code)
            except (ValueError, AttributeError):
                if part in ALL_STRATEGY_CODES and part not in result:
                    result.append(part)
        else:
            if part in ALL_STRATEGY_CODES and part not in result:
                result.append(part)
    
    return result if result else ALL_STRATEGY_CODES


# ============================================================
# PERFORMANCE TRACKING
# ============================================================

@dataclass
class StrategyPerformance:
    """
    Track strategy performance for adaptive sizing and ranking.
    
    Thread-safe update logic.
    """
    
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    pnl_history: Deque[float] = field(default_factory=lambda: deque(maxlen=1000))
    
    # Extremes
    max_win: float = 0.0
    max_loss: float = 0.0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    
    # Streaks
    current_win_streak: int = 0
    current_loss_streak: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    
    # Regime tracking
    regime_performance: Dict[str, Dict[str, float]] = field(default_factory=dict)
    
    # Lock for thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0
    
    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.trades if self.trades > 0 else 0.0
    
    @property
    def avg_win(self) -> float:
        return self.gross_profit / self.wins if self.wins > 0 else 0.0
    
    @property
    def avg_loss(self) -> float:
        return self.gross_loss / self.losses if self.losses > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float('inf') if self.gross_profit > 0 else 0.0
        return self.gross_profit / abs(self.gross_loss)
    
    @property
    def expectancy(self) -> float:
        return (self.win_rate * self.avg_win) + ((1 - self.win_rate) * self.avg_loss)
    
    @property
    def sharpe_estimate(self) -> float:
        if len(self.pnl_history) < 10:
            return 0.0
        
        returns = np.array(self.pnl_history)
        mean_ret = np.mean(returns)
        std_ret = np.std(returns)
        
        if std_ret == 0:
            return 0.0
        
        # Annualized assuming ~50 trades/day (rough estimate)
        return (mean_ret / std_ret) * np.sqrt(50 * 252)
    
    def update(
        self,
        pnl: float,
        initial_equity: float = 10000.0,
        regime: Optional[str] = None,
    ) -> None:
        """Update performance with thread safety."""
        with self._lock:
            self.trades += 1
            self.total_pnl += pnl
            self.pnl_history.append(pnl)
            
            if pnl > 0:
                self.wins += 1
                self.gross_profit += pnl
                self.max_win = max(self.max_win, pnl)
                self.current_win_streak += 1
                self.current_loss_streak = 0
                self.max_win_streak = max(self.max_win_streak, self.current_win_streak)
            else:
                self.losses += 1
                self.gross_loss += pnl
                self.max_loss = min(self.max_loss, pnl)
                self.current_loss_streak += 1
                self.current_win_streak = 0
                self.max_loss_streak = max(self.max_loss_streak, self.current_loss_streak)
            
            # Equity & Drawdown
            if self.current_equity == 0:
                self.current_equity = initial_equity
                self.peak_equity = initial_equity
            
            self.current_equity += pnl
            self.peak_equity = max(self.peak_equity, self.current_equity)
            
            if self.peak_equity > 0:
                current_dd = (self.peak_equity - self.current_equity) / self.peak_equity
                self.max_drawdown = max(self.max_drawdown, current_dd)
            
            # Regime tracking
            if regime:
                if regime not in self.regime_performance:
                    self.regime_performance[regime] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
                self.regime_performance[regime]['trades'] += 1
                self.regime_performance[regime]['pnl'] += pnl
                if pnl > 0:
                    self.regime_performance[regime]['wins'] += 1
    
    def recent_win_rate(self, n: int = 50) -> float:
        with self._lock:
            if not self.pnl_history:
                return 0.0
            # Convert deque to list for slicing
            recent = list(self.pnl_history)[-n:]
            return sum(1 for p in recent if p > 0) / len(recent)
    
    def reset(self) -> None:
        """Reset all statistics."""
        with self._lock:
            self.trades = 0
            self.wins = 0
            self.losses = 0
            self.total_pnl = 0.0
            self.gross_profit = 0.0
            self.gross_loss = 0.0
            self.pnl_history.clear()
            self.max_win = 0.0
            self.max_loss = 0.0
            self.max_drawdown = 0.0
            self.peak_equity = 0.0
            self.current_equity = 0.0
            self.current_win_streak = 0
            self.current_loss_streak = 0
            self.max_win_streak = 0
            self.max_loss_streak = 0
            self.regime_performance.clear()
    
    def to_dict(self) -> Dict[str, Any]:
        """Export as dictionary."""
        with self._lock:
            pf = self.profit_factor
            return {
                'trades': self.trades,
                'wins': self.wins,
                'losses': self.losses,
                'win_rate': round(self.win_rate, 4),
                'total_pnl': round(self.total_pnl, 6),
                'profit_factor': round(pf, 4) if pf != float('inf') else 999.0,
                'max_drawdown': round(self.max_drawdown, 4),
                'sharpe_estimate': round(self.sharpe_estimate, 4),
                'regime_performance': self.regime_performance.copy(),
            }


# ============================================================
# STRATEGY MANAGER (Optimized)
# ============================================================

class StrategyManager:
    """
    Manage all strategies with centralized metric computation.
    
    Key features:
    - Single precomputation pass per candle
    - Lazy pattern evaluation
    - Regime filtering
    - Consensus boosting
    - Performance tracking
    """
    
    __slots__ = (
        'config',
        'strategies',
        'performance',
        '_pattern_detector',
        '_regime_detector',
        'position_sizer',
        '_last_regime',
    )
    
    def __init__(
        self,
        config: StrategyConfig,
        enabled_codes: Optional[Union[List[str], str]] = None,
    ) -> None:
        """Initialize strategy manager."""
        self.config = config
        self.strategies: Dict[str, BaseStrategy] = {}
        self.performance: Dict[str, StrategyPerformance] = {}
        self._last_regime = MarketRegime.UNKNOWN
        
        # Parse enabled strategies
        if enabled_codes is None:
            codes_to_enable = ALL_STRATEGY_CODES
        elif isinstance(enabled_codes, str):
            codes_to_enable = parse_strategy_filter(enabled_codes)
        else:
            codes_to_enable = enabled_codes
        
        # Initialize strategies
        for cls in STRATEGY_CLASSES:
            if cls.code in codes_to_enable:
                if not self.config.is_strategy_enabled(cls.code):
                    continue
                
                strategy = cls(config)
                self.strategies[strategy.code] = strategy
                self.performance[strategy.code] = StrategyPerformance()
        
        # Shared components
        self._pattern_detector = CandlePatternDetector(doji_body_pct=config.doji_body_pct)
        self._regime_detector = MarketRegimeDetector()
        self.position_sizer = PositionSizer(config)
        
        logger.info(
            f"StrategyManager initialized: {len(self.strategies)} strategies"
        )
    
    @property
    def enabled_strategies(self) -> List[str]:
        return sorted(self.strategies.keys())
    
    @property
    def strategy_count(self) -> int:
        return len(self.strategies)
    
    def evaluate_all(
        self,
        band_low: float,
        band_high: float,
        ticks: List[TickTuple],
        candle_data: Optional[pd.DataFrame] = None,
        order_book: Any = None,
        tick_sizes: Optional[List[float]] = None,
    ) -> Dict[str, Optional[TradeSignal]]:
        """
        Evaluate all strategies for a single candle/tick-batch.
        
        Returns:
            {strategy_code: TradeSignal | None}
        """
        # 1. Validation and fast exit
        if not ticks:
            return {code: None for code in self.strategies}
        
        if band_low <= 0 or band_high <= 0 or band_high <= band_low:
            return {code: None for code in self.strategies}
        
        # Band width check
        band_pct = (band_high - band_low) / band_low * 100.0
        if band_pct < self.config.min_band_pct:
            return {code: None for code in self.strategies}
        
        # 2. Create data objects
        band = BandData(low=band_low, high=band_high)
        tick_analysis = TickAnalysis(
            ticks=ticks,
            band=band,
            order_book=order_book,
            candle_data=candle_data,
            tick_sizes=tick_sizes,
        )
        
        # 3. Precompute metrics (ONCE)
        metrics = tick_analysis.precompute(self.config)
        self._last_regime = metrics.regime
        
        # 4. Evaluate strategies
        results: Dict[str, Optional[TradeSignal]] = {}
        
        for code, strategy in self.strategies.items():
            # Check regime filter
            if self.config.use_regime_filter and self.config.skip_unfavorable_regimes:
                if strategy.favorable_regimes and metrics.regime not in strategy.favorable_regimes:
                    results[code] = None
                    continue
            
            # Inject tick analysis for lazy pattern computation
            strategy.set_tick_analysis(tick_analysis)
            
            try:
                signal = strategy.evaluate(band, metrics)
                results[code] = signal
            except Exception as e:
                logger.error(f"Strategy {code} failed: {e}", exc_info=True)
                results[code] = None
        
        # 5. Apply consensus boost (if enabled)
        if self.config.use_consensus_boost:
            results = self._apply_consensus_boost(results)
        
        return results
    
    def _apply_consensus_boost(
        self,
        signals: Dict[str, Optional[TradeSignal]],
    ) -> Dict[str, Optional[TradeSignal]]:
        """Apply confidence boost when multiple strategies agree."""
        active = [(code, sig) for code, sig in signals.items() if sig is not None]
        
        if len(active) < self.config.min_strategies_for_consensus:
            return signals
        
        # Count by direction
        long_count = sum(1 for _, s in active if s.action == TradeAction.LONG)
        short_count = sum(1 for _, s in active if s.action == TradeAction.SHORT)
        total = long_count + short_count
        
        if total == 0:
            return signals
        
        # Determine consensus direction
        long_ratio = long_count / total
        short_ratio = short_count / total
        
        boost = 0.0
        boost_direction = TradeAction.NONE
        
        if long_ratio >= self.config.consensus_ratio_threshold:
            boost_direction = TradeAction.LONG
            if long_ratio >= 0.8 and long_count >= 3:
                boost = StrategyConstants.CONSENSUS_BOOST_STRONG
            else:
                boost = StrategyConstants.CONSENSUS_BOOST_MODERATE
        
        elif short_ratio >= self.config.consensus_ratio_threshold:
            boost_direction = TradeAction.SHORT
            if short_ratio >= 0.8 and short_count >= 3:
                boost = StrategyConstants.CONSENSUS_BOOST_STRONG
            else:
                boost = StrategyConstants.CONSENSUS_BOOST_MODERATE
        
        # Apply boost to matching signals
        if boost > 0 and boost_direction != TradeAction.NONE:
            for _, signal in active:
                if signal.action == boost_direction:
                    signal.confidence = min(
                        0.95,  # Hard cap
                        signal.confidence + boost
                    )
                    signal.metadata['consensus_boost'] = boost
                    signal.metadata['consensus_count'] = (
                        long_count if boost_direction == TradeAction.LONG else short_count
                    )
        
        return signals
    
    def get_all_active_signals(
        self,
        signals: Dict[str, Optional[TradeSignal]],
    ) -> List[Tuple[str, TradeSignal]]:
        """Get list of valid signals sorted by confidence."""
        active = [
            (code, sig) for code, sig in signals.items()
            if sig is not None and sig.is_valid()
        ]
        # Sort by confidence descending
        active.sort(key=lambda x: x[1].confidence, reverse=True)
        return active
    
    def get_best_signal(
        self,
        signals: Dict[str, Optional[TradeSignal]],
        prefer_direction: Optional[TradeAction] = None,
        use_performance_weighting: bool = True,
    ) -> Optional[Tuple[str, TradeSignal]]:
        """
        Select the single best signal.
        
        Uses performance history to weight confidence if enabled.
        """
        active = self.get_all_active_signals(signals)
        
        if not active:
            return None
        
        # Filter by direction preference
        if prefer_direction and prefer_direction != TradeAction.NONE:
            active = [s for s in active if s[1].action == prefer_direction]
            if not active:
                return None
        
        if not use_performance_weighting:
            return active[0]  # Return highest confidence
        
        # Apply performance weighting
        scored_signals = []
        for code, signal in active:
            perf = self.performance.get(code)
            score = signal.confidence
            
            if perf and perf.trades >= 10:
                # Weight: 70% confidence + 20% win rate + 10% profit factor
                wr_bonus = (perf.recent_win_rate(50) - 0.5) * 0.4
                pf_bonus = min(0.1, (perf.profit_factor - 1) * 0.05) if perf.profit_factor < float('inf') else 0.1
                
                score = score * 0.7 + (0.5 + wr_bonus) * 0.2 + pf_bonus
            
            scored_signals.append((score, code, signal))
        
        scored_signals.sort(key=lambda x: x[0], reverse=True)
        return (scored_signals[0][1], scored_signals[0][2])
    
    def update_performance(
        self,
        code: str,
        pnl: float,
        initial_equity: float = 10000.0,
        regime: Optional[str] = None,
    ) -> None:
        """Update strategy performance stats."""
        if code in self.performance:
            self.performance[code].update(pnl, initial_equity, regime)
    
    def get_performance_report(self) -> pd.DataFrame:
        """Generate performance summary DataFrame."""
        rows = []
        
        for code, perf in self.performance.items():
            if perf.trades == 0:
                continue
            
            rows.append({
                "Strategy": code,
                "Trades": perf.trades,
                "WinRate%": round(perf.win_rate * 100, 1),
                "TotalPnL": round(perf.total_pnl, 4),
                "AvgPnL": round(perf.avg_pnl, 6),
                "PF": round(perf.profit_factor, 2) if perf.profit_factor != float('inf') else 999.0,
                "MaxDD%": round(perf.max_drawdown * 100, 1),
                "Sharpe": round(perf.sharpe_estimate, 2),
            })
        
        if not rows:
            return pd.DataFrame()
        
        df = pd.DataFrame(rows)
        return df.sort_values("TotalPnL", ascending=False).reset_index(drop=True)
    
    def reset_performance(self, code: Optional[str] = None) -> None:
        """Reset performance stats."""
        if code:
            if code in self.performance:
                self.performance[code].reset()
        else:
            for perf in self.performance.values():
                perf.reset()
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize state."""
        return {
            'config': self.config.to_dict(),
            'enabled_strategies': self.enabled_strategies,
            'performance': {
                code: perf.to_dict()
                for code, perf in self.performance.items()
            },
        }


# ============================================================
# FACTORY FUNCTIONS
# ============================================================

def create_strategy_manager(
    config_dict: Optional[Dict[str, Any]] = None,
    enabled_strategies: Optional[Union[List[str], str]] = None,
) -> StrategyManager:
    """Factory to create StrategyManager."""
    if config_dict:
        config = StrategyConfig.from_dict(config_dict)
    else:
        config = StrategyConfig()
    
    return StrategyManager(config, enabled_codes=enabled_strategies)


# ============================================================
# EXPORTS
# ============================================================

__all__ = [
    'StrategyConfig',
    'StrategyParams',
    'StrategyManager',
    'BaseStrategy',
    'TradeSignal',
    'TradeAction',
    'MarketRegime',
    'PositionSizingMethod',
    'CandlePattern',
    'PrecomputedMetrics',
    'BandData',
    'TickAnalysis',
    'OrderBookWrapper',
    'ALL_STRATEGY_CODES',
    'create_strategy_manager',
    'get_strategy_info',
    'parse_strategy_filter',
    # Strategies
    'S1_PathClear',
    'S2_MeanReversion',
    'S3_MultiTouch',
    'S4_SimpleBandEntry',
    'S5_VolatilitySqueeze',
    'S6_TickClustering',
    'S7_FakeoutRecovery',
    'S8_OrderFlowMomentum',
    'S9_WallFade',
    'S10_SpreadNormalization',
    'S11_CandlePattern',
    'S12_StopHunt',
    'S13_IcebergDetector',
    'S14_LiquiditySweep',
    'S15_BotExhaustion',
    'S16_ImbalanceDivergence',
    'S17_MomentumTrap',
    'S18_VWAPDeviation',
    'S19_AbsorptionDetector',
    'S20_DepthGradient',
    'S21_QueueImbalance',
    'S22_InstitutionalFlow',
    'S23_MarketMakingEdge',
]
