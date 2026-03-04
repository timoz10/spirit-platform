"""
Pending Order Manager — tracks limit orders that have been placed but not yet filled.

Separate from TradeStateManager because a pending order is not yet a trade.
Once filled, the order transitions to an open trade in the normal TSM flow.

State machine:
    NO_POSITION -> (strategy signal) -> LIMIT_PENDING
    LIMIT_PENDING -> (filled)  -> OPEN_TRADE (normal TSM flow)
    LIMIT_PENDING -> (expired) -> NO_POSITION (cancel on exchange, resume scanning)
    LIMIT_PENDING -> (cancel)  -> NO_POSITION
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from spirit.logger import get_logger

logger = get_logger("pending_orders")


@dataclass
class PendingLimitOrder:
    """A limit order placed on the exchange but not yet filled."""
    pair: str
    txid: str                       # Exchange order ID (or paper ID)
    limit_price: float              # Target fill price
    zone_id: Optional[int] = None
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_minutes: int = 60
    ttl_bars: int = 0               # Bar-based TTL (0 = use time-based only)
    bars_elapsed: int = 0           # Incremented each tick_bar() call
    buy_amount_usd: float = 0.0
    volume: float = 0.0
    source: str = 'confirmed'       # 'confirmed' | 'predictive'
    signal_context: Dict = field(default_factory=dict)  # TradeSignal + RiskDecision for fill handoff

    @property
    def is_expired(self) -> bool:
        """True if order has exceeded its TTL (time-based or bar-based)."""
        # Bar-based expiry (for replay mode and predictive entries)
        if self.ttl_bars > 0 and self.bars_elapsed >= self.ttl_bars:
            return True
        # Time-based expiry (for live/paper mode)
        age = (datetime.now(timezone.utc) - self.placed_at).total_seconds() / 60
        return age >= self.ttl_minutes

    @property
    def age_minutes(self) -> float:
        """Minutes since order was placed."""
        return (datetime.now(timezone.utc) - self.placed_at).total_seconds() / 60

    def tick_bar(self) -> bool:
        """Increment bar counter. Returns True if now expired."""
        self.bars_elapsed += 1
        return self.is_expired


class PendingOrderManager:
    """Tracks pending limit orders, one per pair."""

    def __init__(self):
        self._pending: Dict[str, PendingLimitOrder] = {}

    def has_pending(self, pair: str) -> bool:
        return pair in self._pending

    def get_pending(self, pair: str) -> Optional[PendingLimitOrder]:
        return self._pending.get(pair)

    def place(self, order: PendingLimitOrder) -> None:
        """Register a new pending order. Raises if one already exists for this pair."""
        if order.pair in self._pending:
            raise ValueError(
                f"Already have pending order for {order.pair}: "
                f"txid={self._pending[order.pair].txid}"
            )
        self._pending[order.pair] = order
        ttl_info = f"ttl={order.ttl_minutes}m"
        if order.ttl_bars > 0:
            ttl_info += f" ttl_bars={order.ttl_bars}"
        logger.info(
            f"[{order.pair}][LIMIT_PLACED] txid={order.txid} "
            f"price={order.limit_price:.2f} {ttl_info} "
            f"zone_id={order.zone_id} source={order.source}"
        )

    def remove(self, pair: str) -> Optional[PendingLimitOrder]:
        """Remove and return the pending order for a pair, or None."""
        return self._pending.pop(pair, None)

    def all_pending(self) -> Dict[str, PendingLimitOrder]:
        """Return a copy of all pending orders."""
        return dict(self._pending)
