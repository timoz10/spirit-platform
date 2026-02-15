"""
Signal contract between Algorithm and Risk/Execution layers.

TradeSignal: Algorithm output — what the strategy detected
RiskDecision: RiskGate output — whether to trade and at what size

This module defines the interface between Spirit's modular layers:
  Algorithm Plugin  →  TradeSignal  →  RiskGate  →  RiskDecision  →  Execution
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class SignalAction(Enum):
    ENTRY = 'ENTRY'
    EXIT = 'EXIT'
    NONE = 'NONE'


@dataclass
class TradeSignal:
    """Signal contract between Algorithm and Risk/Execution layers.

    The Algorithm plugin emits this on each candle. RiskGate consumes it
    to decide sizing and trade/skip. Execution uses the resulting
    RiskDecision to populate TradeRecord.buy_amount.
    """
    action: SignalAction

    # Market context at signal time
    price: float
    datetime: str
    pair: str = 'XBTUSD'
    interval: int = 60

    # Algorithm-provided context (for Risk Module to evaluate)
    confidence_score: Optional[float] = None
    confidence_tier: Optional[str] = None
    zone_id: Optional[int] = None
    regime: Optional[str] = None
    strategy_name: Optional[str] = None

    # D-Limit context
    trend_state: Optional[str] = None
    slope_angle: Optional[float] = None
    capture_rate: Optional[float] = None
    atr_pct: Optional[float] = None
    atr_14: Optional[float] = None
    trend_end_confidence: Optional[float] = None

    # Suggested levels (algorithm's recommendation, risk module may override)
    suggested_stop_pct: Optional[float] = None
    suggested_target_pct: Optional[float] = None

    # Exit context (only when action=EXIT)
    exit_reason: Optional[str] = None

    # Raw row data for TradeRecord construction
    row_data: Dict = field(default_factory=dict)


@dataclass
class RiskDecision:
    """Output from RiskGate — complete trade decision with sizing.

    Contains the full audit trail for decision traceability (Req V2 Section 7.3).
    """
    trade: bool                          # True = execute, False = skip
    skip_reason: Optional[str] = None    # Why skipped

    # Sizing (from PositionSizer)
    position_size_usd: float = 0.0
    position_size_pct: float = 0.0

    # Profile (from RiskRewardProfiler)
    profile_tier: Optional[str] = None   # STRONG/GOOD/MARGINAL/WEAK
    rr_ratio: float = 0.0
    ev_pct: float = 0.0

    # Stops and targets (final, after risk evaluation)
    stop_level_pct: float = 0.0
    profit_target_pct: float = 0.0

    # Multiplier audit trail
    confidence_multiplier: float = 0.0
    profile_multiplier: float = 0.0
    regime_multiplier: float = 1.0

    # Full snapshot for decision traceability (Req V2 Section 7.3)
    snapshot: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Serialize for logging/export."""
        return {
            'trade': self.trade,
            'skip_reason': self.skip_reason,
            'position_size_usd': round(self.position_size_usd, 2),
            'position_size_pct': round(self.position_size_pct, 3),
            'profile_tier': self.profile_tier,
            'rr_ratio': round(self.rr_ratio, 2),
            'ev_pct': round(self.ev_pct, 3),
            'stop_level_pct': round(self.stop_level_pct, 3),
            'profit_target_pct': round(self.profit_target_pct, 3),
            'confidence_multiplier': self.confidence_multiplier,
            'profile_multiplier': self.profile_multiplier,
            'regime_multiplier': round(self.regime_multiplier, 4),
        }
