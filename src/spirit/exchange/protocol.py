"""
ExchangeProvider — Abstract exchange interface for Spirit.

Defines the contract that every exchange plugin must implement.
Spirit never calls exchange APIs directly — all access goes through
this protocol.

Implementations:
  - KrakenExchangeProvider (src/spirit/exchange/kraken.py)
  - Add yours: see docs/reference/EXCHANGE_PLUGIN_GUIDE.md

Usage:
    from spirit.exchange import get_exchange_provider
    ep = get_exchange_provider()
    ticker = ep.get_ticker('XBTUSD')
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# =====================================================================
# Standard return types
# =====================================================================
# Exchange plugins MUST return these dataclasses, not raw dicts.
# This ensures every consumer gets the same shape regardless of
# which exchange is behind the provider.

@dataclass(frozen=True)
class Ticker:
    """Current bid/ask/last for a trading pair."""
    bid: float
    ask: float
    last: float


@dataclass(frozen=True)
class PairInfo:
    """Exchange metadata for a trading pair.

    Attributes:
        pair:            Spirit-normalised name (e.g. 'XBTUSD')
        base_asset:      Base asset symbol (e.g. 'XBT', 'BTC', 'ETH')
        quote_asset:     Quote asset symbol (e.g. 'USD')
        lot_decimals:    Max decimal places for order volume
        price_decimals:  Max decimal places for price
        ordermin:        Minimum order size in base asset
    """
    pair: str
    base_asset: str
    quote_asset: str
    lot_decimals: int
    price_decimals: int
    ordermin: float


@dataclass(frozen=True)
class OrderResult:
    """Result of placing an order.

    Attributes:
        txid:    Exchange transaction/order ID
        status:  'open', 'closed', 'pending', 'canceled', 'expired', 'error'
        price:   Fill price (0.0 if not yet filled)
        volume:  Filled volume (0.0 if not yet filled)
        raw:     Full exchange response for debugging (optional)
    """
    txid: str
    status: str
    price: float = 0.0
    volume: float = 0.0
    raw: dict | None = None


@dataclass(frozen=True)
class OrderStatus:
    """Status of an existing order.

    Attributes:
        txid:           Exchange transaction/order ID
        status:         'open', 'closed', 'canceled', 'expired'
        filled_price:   Average fill price (0.0 if unfilled)
        filled_volume:  Total filled volume (0.0 if unfilled)
        remaining:      Remaining unfilled volume
        raw:            Full exchange response for debugging (optional)
    """
    txid: str
    status: str
    filled_price: float = 0.0
    filled_volume: float = 0.0
    remaining: float = 0.0
    raw: dict | None = None


@dataclass(frozen=True)
class OHLCCandle:
    """A single OHLC candle from the exchange."""
    timestamp: int       # Unix epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float = 0.0
    count: int = 0


@dataclass(frozen=True)
class OrderbookLevel:
    """A single price level in the orderbook."""
    price: float
    volume: float
    timestamp: int = 0


@dataclass(frozen=True)
class Orderbook:
    """Current orderbook depth."""
    asks: list[OrderbookLevel]
    bids: list[OrderbookLevel]


# =====================================================================
# ExchangeProvider Protocol
# =====================================================================

@runtime_checkable
class ExchangeProvider(Protocol):
    """Abstract interface for exchange access.

    Every method uses Spirit-normalised pair names (e.g. 'XBTUSD', 'ETHUSD').
    The implementation handles mapping to exchange-native names internally.

    Methods are grouped into:
      - Public (no auth): get_ticker, get_ohlc, get_pair_info, get_orderbook
      - Private (auth required): place_order, cancel_order, get_order_status,
        get_open_orders, get_balance

    Implementations MUST:
      - Accept Spirit pair names and translate internally
      - Return the standard dataclasses defined above (not raw dicts)
      - Raise RuntimeError on unrecoverable errors (bad credentials, unknown pair)
      - Handle transient errors (rate limits, timeouts) with internal retry
      - Be thread-safe (Spirit calls from multiple pair threads)
    """

    # -----------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------

    @property
    def name(self) -> str:
        """Short exchange name, e.g. 'kraken', 'binance'. Used in logs and config."""
        ...

    # -----------------------------------------------------------------
    # Public market data (no authentication required)
    # -----------------------------------------------------------------

    def get_ticker(self, pair: str) -> Ticker:
        """Current bid/ask/last for a pair.

        Args:
            pair: Spirit pair name, e.g. 'XBTUSD'

        Returns:
            Ticker with bid, ask, last as floats.
        """
        ...

    def get_ohlc(
        self, pair: str, interval: int = 60, count: int = 720
    ) -> list[OHLCCandle]:
        """Fetch recent closed OHLC candles from the exchange.

        Args:
            pair:     Spirit pair name
            interval: Candle interval in minutes (1, 5, 15, 60, etc.)
            count:    Number of candles to return

        Returns:
            List of OHLCCandle, oldest first. Only closed candles —
            the current (incomplete) candle MUST be excluded.
        """
        ...

    def get_pair_info(self, pair: str) -> PairInfo:
        """Pair metadata: lot sizing, order minimum, decimal precision.

        Args:
            pair: Spirit pair name

        Returns:
            PairInfo with ordermin, lot_decimals, price_decimals.

        Implementations should cache this — pair info rarely changes.
        """
        ...

    def get_orderbook(self, pair: str, depth: int = 100) -> Orderbook:
        """Orderbook depth (asks + bids).

        Args:
            pair:  Spirit pair name
            depth: Number of levels per side (exchange may cap this)

        Returns:
            Orderbook with asks (ascending by price) and bids (descending).
        """
        ...

    # -----------------------------------------------------------------
    # Private trading (authentication required)
    # -----------------------------------------------------------------

    def place_order(
        self,
        pair: str,
        side: str,
        volume: float,
        order_type: str = "market",
        price: float | None = None,
        validate_only: bool = False,
    ) -> OrderResult:
        """Submit an order to the exchange.

        Args:
            pair:          Spirit pair name
            side:          'buy' or 'sell'
            volume:        Order size in base asset
            order_type:    'market' or 'limit'
            price:         Required for limit orders
            validate_only: If True, validate without placing (dry run)

        Returns:
            OrderResult with txid and initial status.

        Raises:
            RuntimeError: if credentials missing, pair unknown, or exchange rejects.
        """
        ...

    def cancel_order(self, txid: str) -> bool:
        """Cancel an open order.

        Args:
            txid: Exchange order/transaction ID

        Returns:
            True if canceled, False if already filled or not found.
        """
        ...

    def get_order_status(self, txid: str) -> OrderStatus:
        """Query the current status of an order.

        Args:
            txid: Exchange order/transaction ID

        Returns:
            OrderStatus with fill details.
        """
        ...

    def get_open_orders(self) -> list[OrderStatus]:
        """All currently open orders on the exchange.

        Returns:
            List of OrderStatus for each open order.
        """
        ...

    def get_balance(self) -> dict[str, float]:
        """Available balances per asset.

        Returns:
            Dict mapping asset symbol to available balance.
            e.g. {'USD': 5000.0, 'XBT': 0.5, 'ETH': 10.0}
        """
        ...
