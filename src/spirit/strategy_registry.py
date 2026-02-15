"""
Strategy Registry — Maps intervals to strategies for multi-strategy routing.

Used by SpiritOrchestrator to route each interval's candles to the
strategies that need them. Different strategies can use different signal
intervals and monitoring intervals.

Author: Claude Code + Tim
Date: 2026-02-15
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from spirit.strategies.base import BaseStrategy
from spirit.logger import get_logger

logger = get_logger("strategy_registry")


@dataclass
class StrategySlot:
    """One registered strategy with its routing metadata."""
    name: str                             # "zone_bounce"
    strategy: BaseStrategy
    signal_interval: int                  # 60
    monitoring_intervals: Set[int]        # {1}
    uses_risk_gate: bool
    pairs: List[str]                      # ["XBTUSD", "ETHUSD"]
    warmup_candles: int = 720


class StrategyRegistry:
    """Registry that maps intervals → strategies for candle routing."""

    def __init__(self):
        self._slots: Dict[str, StrategySlot] = {}
        # Index: interval → list of slot names that need this interval as signal
        self._signal_index: Dict[int, List[str]] = {}
        # Index: interval → list of slot names that need this interval for monitoring
        self._monitor_index: Dict[int, List[str]] = {}

    def register(self, name: str, strategy: BaseStrategy, pairs: List[str]) -> StrategySlot:
        """Register a strategy and build routing indexes.

        Args:
            name: Unique strategy name (e.g. "zone_bounce")
            strategy: BaseStrategy instance
            pairs: Which pairs this strategy trades

        Returns:
            The created StrategySlot
        """
        reqs = strategy.get_data_requirements()
        slot = StrategySlot(
            name=name,
            strategy=strategy,
            signal_interval=reqs.signal_interval,
            monitoring_intervals=set(reqs.monitoring_intervals),
            uses_risk_gate=strategy.uses_risk_gate,
            pairs=list(pairs),
            warmup_candles=reqs.warmup_candles,
        )
        self._slots[name] = slot

        # Build signal index
        self._signal_index.setdefault(slot.signal_interval, [])
        if name not in self._signal_index[slot.signal_interval]:
            self._signal_index[slot.signal_interval].append(name)

        # Build monitor index
        for mi in slot.monitoring_intervals:
            self._monitor_index.setdefault(mi, [])
            if name not in self._monitor_index[mi]:
                self._monitor_index[mi].append(name)

        logger.info(
            f"[REGISTRY] Registered '{name}': signal={slot.signal_interval}m "
            f"monitoring={sorted(slot.monitoring_intervals)} "
            f"pairs={slot.pairs} risk_gate={slot.uses_risk_gate}"
        )
        return slot

    def get_signal_strategies(self, interval: int) -> List[StrategySlot]:
        """Which strategies need this interval as their signal interval?"""
        names = self._signal_index.get(interval, [])
        return [self._slots[n] for n in names if n in self._slots]

    def get_monitor_strategies(self, interval: int) -> List[StrategySlot]:
        """Which strategies need this interval for monitoring?"""
        names = self._monitor_index.get(interval, [])
        return [self._slots[n] for n in names if n in self._slots]

    def all_intervals(self) -> Set[int]:
        """Union of all signal + monitoring intervals across all strategies."""
        intervals = set()
        for slot in self._slots.values():
            intervals.add(slot.signal_interval)
            intervals.update(slot.monitoring_intervals)
        return intervals

    def all_pairs(self) -> List[str]:
        """Union of all pairs across all strategies (deduplicated, sorted)."""
        pairs = set()
        for slot in self._slots.values():
            pairs.update(slot.pairs)
        return sorted(pairs)

    def max_warmup(self) -> int:
        """Maximum warmup candles needed across all strategies."""
        if not self._slots:
            return 720
        return max(s.warmup_candles for s in self._slots.values())

    def slots_for_pair(self, pair: str) -> List[StrategySlot]:
        """All strategy slots that trade a given pair."""
        return [s for s in self._slots.values() if pair in s.pairs]

    def get_slot(self, name: str) -> Optional[StrategySlot]:
        """Get a slot by strategy name."""
        return self._slots.get(name)

    def __len__(self) -> int:
        return len(self._slots)

    def __contains__(self, name: str) -> bool:
        return name in self._slots
