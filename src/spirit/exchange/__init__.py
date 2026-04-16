"""
Spirit Exchange Plugin System

Provides a standard interface for exchange access. Spirit never calls
exchange APIs directly — all access goes through get_exchange_provider().

Config:
    SPIRIT_EXCHANGE: kraken      # Exchange to use (spirit.yaml or env)
    EXCHANGE_API_KEY: ...        # Generic credential (env only)
    EXCHANGE_API_SECRET: ...     # Generic credential (env only)

Backward compat:
    KRAKEN_API_KEY / KRAKEN_API_SECRET are accepted as fallback if the
    generic names are not set.

Usage:
    from spirit.exchange import get_exchange_provider
    ep = get_exchange_provider()
    ticker = ep.get_ticker('XBTUSD')
"""

from __future__ import annotations

import os
from typing import Optional

from spirit.exchange.protocol import (
    ExchangeProvider,
    Ticker,
    PairInfo,
    OrderResult,
    OrderStatus,
    OHLCCandle,
    OrderbookLevel,
    Orderbook,
)
from spirit.logger import get_logger

logger = get_logger("exchange")

__all__ = [
    "get_exchange_provider",
    "ExchangeProvider",
    "Ticker",
    "PairInfo",
    "OrderResult",
    "OrderStatus",
    "OHLCCandle",
    "OrderbookLevel",
    "Orderbook",
]

_provider: Optional[ExchangeProvider] = None


def get_exchange_provider() -> ExchangeProvider:
    """Return the singleton ExchangeProvider instance.

    Created on first call based on SPIRIT_EXCHANGE config:
      - 'kraken' (default): KrakenExchangeProvider
      - Future: 'binance', 'coinbase', etc.

    To add a new exchange, create a class implementing ExchangeProvider
    and add it to the if/elif chain below. See
    docs/reference/EXCHANGE_PLUGIN_GUIDE.md for the full contract.
    """
    global _provider
    if _provider is not None:
        return _provider

    from spirit.utils.config_loader import get_config

    exchange = get_config("SPIRIT_EXCHANGE", "")
    if not exchange:
        exchange = os.environ.get("SPIRIT_EXCHANGE", "kraken")

    if exchange == "kraken":
        from spirit.exchange.kraken import KrakenExchangeProvider
        _provider = KrakenExchangeProvider()
    else:
        raise RuntimeError(
            f"Unknown exchange: '{exchange}'. "
            f"Set SPIRIT_EXCHANGE to a supported exchange (kraken). "
            f"See docs/reference/EXCHANGE_PLUGIN_GUIDE.md for adding new exchanges."
        )

    logger.info(f"[EXCHANGE] Provider: {_provider.name}")
    return _provider


def reset_provider() -> None:
    """Reset the singleton (for testing only)."""
    global _provider
    _provider = None
