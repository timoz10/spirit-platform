"""
ContextManager — Manages per-pair SpiritContext instances.

Provides a single entry point for multi-pair state management.
Each pair gets its own SpiritContext (own ohlc_df, own open_trade).
Equity is shared across all pairs (one account balance).
"""

from typing import Dict, List, Optional

from spirit.context import SpiritContext
from spirit.logger import get_logger

logger = get_logger("context_manager")


class ContextManager:
    """Manage Dict[str, SpiritContext] for multi-pair operation."""

    def __init__(self, pairs: List[str], persist_to_pg: bool = True, max_rows: int = 720):
        """
        Args:
            pairs: List of trading pair symbols (e.g. ['XBTUSD', 'ETHUSD'])
            persist_to_pg: Whether to persist state to PostgreSQL
            max_rows: Maximum OHLC rows per interval per pair
        """
        self.pairs = list(pairs)
        self._contexts: Dict[str, SpiritContext] = {}
        for pair in self.pairs:
            self._contexts[pair] = SpiritContext(
                pair=pair,
                max_rows=max_rows,
                persist_to_pg=persist_to_pg,
            )
        logger.info(f"[ContextManager] Initialized for pairs: {self.pairs}")

    def get(self, pair: str) -> SpiritContext:
        """Get the SpiritContext for a specific pair."""
        if pair not in self._contexts:
            raise KeyError(f"No context for pair '{pair}'. Available: {list(self._contexts.keys())}")
        return self._contexts[pair]

    def all_pairs(self) -> List[str]:
        """Return list of managed pair symbols."""
        return list(self._contexts.keys())

    def save_all(self):
        """Persist state for all pairs to PG."""
        for pair, ctx in self._contexts.items():
            try:
                ctx.save_state()
            except Exception as e:
                logger.error(f"[ContextManager] Failed to save state for {pair}: {e}")

    def restore_all(self):
        """Restore state for all pairs from PG."""
        for pair, ctx in self._contexts.items():
            try:
                ctx.restore_state()
            except Exception as e:
                logger.error(f"[ContextManager] Failed to restore state for {pair}: {e}")

    def __getitem__(self, pair: str) -> SpiritContext:
        """Dict-style access: context_manager['XBTUSD']."""
        return self.get(pair)

    def __contains__(self, pair: str) -> bool:
        return pair in self._contexts

    def __len__(self) -> int:
        return len(self._contexts)
