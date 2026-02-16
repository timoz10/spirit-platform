"""
BaseStrategy: Abstract base class for all trading strategies.
Defines the interface for entry/exit logic and data requirements.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class DataRequirements:
    """Declares a strategy's data needs at startup.

    spirit_main reads these to create the right data sources,
    set warmup size, and route monitoring ticks.
    """
    pairs: List[str] = field(default_factory=lambda: ['XBTUSD'])
    signal_interval: int = 60            # Triggers evaluate_trade()
    monitoring_intervals: List[int] = field(default_factory=list)  # e.g. [1] for 1m stop checks
    warmup_candles: int = 720

    @property
    def all_intervals(self) -> List[int]:
        return sorted(set([self.signal_interval] + self.monitoring_intervals))

    @property
    def needs_multi_interval(self) -> bool:
        return len(self.all_intervals) > 1


class BaseStrategy(ABC):
    @abstractmethod
    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        """
        Main strategy interface for trade decision logic.
        Must return a standardized dict: {"entry": bool, "exit": bool, "details": {...}}

        Args:
            pair: Trading pair symbol (e.g. 'XBTUSD') — REQUIRED.
            mode: "test", "paper", or "live"
            **kwargs: Additional context from the orchestrator:
                - open_trade: TradeRecord or None — the current open trade state

        For compatibility with trade_logic:
        - In test mode, details MUST include the row's 'close' price.
        - In live mode, price assignment is handled after order execution.

        Example:
            return {
                "entry": True,
                "exit": False,
                "details": {
                    "datetime": row["datetime"],
                    "entry_price": row["close"],
                    "symbol": pair,
                }
            }
        """
        pass

    def get_data_requirements(self) -> DataRequirements:
        """Declare data needs at startup.

        Default reads self.filter_pair / self.filter_interval if they exist,
        otherwise returns XBTUSD / 60.  Subclasses override for monitoring
        intervals, custom warmup, etc.
        """
        pair = getattr(self, 'filter_pair', None) or 'XBTUSD'
        interval = getattr(self, 'filter_interval', None) or 60
        return DataRequirements(
            pairs=[pair],
            signal_interval=int(interval),
            warmup_candles=720,
        )

    def on_monitoring_tick(self, pair: str, interval: int, candle: dict, open_trade) -> Optional[dict]:
        """Handle sub-signal monitoring ticks (e.g. 1m ATR stop checks).

        Called for each monitoring_interval candle while a trade is open.
        Return an exit dict {"exit": True, "details": {...}} to trigger exit,
        or None to do nothing.

        Args:
            pair: Trading pair symbol
            interval: Monitoring interval (minutes)
            candle: Latest candle dict
            open_trade: Current open TradeRecord
        """
        return None

    def validate_readiness(self) -> Tuple[bool, List[str]]:
        """Post-warmup green-light check.

        Returns (ready, issues) where issues is a list of warning strings.
        Called after warmup; spirit_main logs GREEN/YELLOW LIGHT accordingly.
        """
        return (True, [])

    def on_entry_confirmed(self, pair: str, signal, risk_decision) -> None:
        """Called after RiskGate approves entry. Capture entry context for exit logic."""
        pass

    @property
    def uses_risk_gate(self) -> bool:
        """Whether entries from this strategy route through RiskGate for sizing."""
        return False
