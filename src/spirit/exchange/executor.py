"""
OrderExecutor ABC — Spirit-level order management.

Defines the contract that all order executors must satisfy.
Implementations:
  - LiveOrderExecutor  (real fills via ExchangeProvider)
  - PaperOrderExecutor (simulated fills from candle data)

Strategies and the orchestrator depend on this interface, never on
a concrete executor class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class OrderExecutor(ABC):
    """Abstract base for Spirit order executors.

    Upstream Dependencies:
      - ExchangeProvider: live executors delegate to an ExchangeProvider
        for ticker, order placement, and status queries.

    Outputs:
      - Trade lifecycle methods (place_order, close_order, etc.)
      - equity property for portfolio-level risk management
    """

    def __init__(
        self,
        pair: str = 'XBTUSD',
        pair_info: Optional[dict] = None,
        starting_equity: float = 10000.0,
        run_id: str = 'live',
    ):
        self.pair = pair
        self._pair_info = pair_info or {}
        self.run_id = run_id

    # ------------------------------------------------------------------
    # Shared concrete helpers
    # ------------------------------------------------------------------

    def _round_volume(self, volume: float, pair: str = None) -> float:
        """Round *volume* down to the exchange's lot-size step.

        Uses ``lot_decimals`` from pair_info (default 8).
        """
        if volume is None:
            return None
        p = pair or self.pair
        decimals = self._pair_info.get(p, {}).get('lot_decimals', 8)
        step = 10 ** (-decimals)
        steps = int(volume / step)
        rounded = max(step, steps * step)
        return float(f"{rounded:.10f}")

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def equity(self) -> float:
        """Current portfolio value (cash + unrealised positions)."""
        ...

    @equity.setter
    @abstractmethod
    def equity(self, value: float) -> None:
        """Set equity (used by crash-recovery restore)."""
        ...

    @abstractmethod
    def place_order(self, trade_record) -> dict:
        """Place a market buy, wait for fill, update *trade_record*.

        Returns dict with at least ``{'txid': [...], ...}``.
        """
        ...

    @abstractmethod
    def close_order(self, open_trade, trade_record) -> dict:
        """Close position (market sell), compute PnL, update *trade_record*.

        Returns dict with at least ``{'txid': [...], ...}``.
        """
        ...

    @abstractmethod
    def place_limit_order(self, trade_record, limit_price: float) -> dict:
        """Submit a limit buy — returns immediately (no fill wait).

        Returns dict with at least ``{'txid': [...], ...}``.
        """
        ...

    @abstractmethod
    def check_order_status(self, txid: str, candle: Optional[dict] = None) -> dict:
        """Query fill status for a pending order.

        Live executors query the exchange (ignoring *candle*).
        Paper executors use *candle* low to simulate fills.

        Returns ``{'status': str, 'fill_price': ..., 'fill_volume': ...,
                   'fill_fee': ..., 'fill_cost': ...}``.
        """
        ...

    @abstractmethod
    def cancel_order(self, txid: str) -> bool:
        """Cancel an unfilled limit order. Returns True on success."""
        ...

    @abstractmethod
    def finalize_limit_fill(self, txid: str, trade_record) -> None:
        """Post-fill bookkeeping: update equity, trade_record, PG records."""
        ...
