"""ExchangeBackedDataProvider — read-side OHLC + pair registry for Free tier (#561).

Composes any `ExchangeProvider` (protocol at `spirit.exchange.protocol`) and
adapts its public market-data methods to the `FrameworkDataProvider` shape
that `context.py`, indicators, and strategies expect. Vendor-specific
behaviour (Kraken pair aliasing, Binance pagination, etc.) lives inside
the `ExchangeProvider` impl — this adapter is brand-agnostic by design.

Per the project naming convention (memory:
feedback_brand_names_at_adapter_boundary.md), there is exactly one place
in the codebase that knows about a vendor: `src/spirit/exchange/<vendor>.py`.
This file knows none.

Scope (intentionally partial)
-----------------------------
This provider implements only the read side of FrameworkDataProvider:

    get_ohlc(pair, interval, *, start, end, limit, order)
    get_pairs(instance)

State, performance, and heartbeat writes are owned by `SqliteDataProvider`.
On Day 4, `CompositeDataProvider` bolts them together so the union
satisfies the full `FrameworkDataProvider` Protocol.

Rule 11 contract
----------------
The framework `get_ohlc` shape is `list[dict]` with keys
`pair, interval, datetime, open, high, low, close, vwap, volume, count`.
`datetime` MUST be tz-aware UTC; all numeric fields MUST be `float`
(except `count` which is `int`). The `OHLCCandle` dataclass returned by
`ExchangeProvider.get_ohlc` carries an integer epoch `timestamp`; this
adapter converts it explicitly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spirit.exchange.protocol import ExchangeProvider
from spirit.logger import get_logger

logger = get_logger("exchange_backed_data_provider")


_PAIRS_PATH = Path(__file__).resolve().parent.parent / "storage" / "pairs.json"


def _ohlc_candle_to_dict(candle, pair: str, interval: int) -> dict:
    """Adapt a single ExchangeProvider OHLCCandle to the framework dict.

    Lifted out of the class so it's trivially testable in isolation and
    so pagination loops in future provider impls can reuse it.
    """
    return {
        "pair": pair,
        "interval": interval,
        "datetime": datetime.fromtimestamp(candle.timestamp, tz=timezone.utc),
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": float(candle.close),
        "vwap": float(candle.vwap),
        "volume": float(candle.volume),
        "count": int(candle.count),
    }


class ExchangeBackedDataProvider:
    """Read-side data provider that delegates to any ExchangeProvider.

    Construction:
        from spirit.exchange.kraken import KrakenExchangeProvider
        from spirit.utils.exchange_backed_data_provider import \\
            ExchangeBackedDataProvider
        ep = KrakenExchangeProvider()
        dp = ExchangeBackedDataProvider(ep)

    The exchange instance is held by reference — its caches (pair info,
    rate-limit token bucket) survive across provider calls.
    """

    def __init__(
        self,
        exchange: ExchangeProvider,
        *,
        pairs: list[dict] | None = None,
    ) -> None:
        self._exchange = exchange
        # Lazy-loaded from the bundled JSON; explicit override accepted for
        # tests + advanced users running on alt exchanges.
        self._pairs_override = pairs
        self._pairs_cache: list[dict] | None = None
        logger.info(
            f"ExchangeBackedDataProvider initialised "
            f"(exchange={exchange.name})"
        )

    # ------------------------------------------------------------------
    # OHLC
    # ------------------------------------------------------------------

    def get_ohlc(
        self,
        pair: str,
        interval: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
        order: str = "asc",
    ) -> list[dict]:
        """Fetch closed OHLC candles, adapted to the framework dict shape.

        Filtering and ordering happen client-side after the exchange
        responds. Bulk backfills (>720 candles for Kraken) are not in
        Free-tier scope; consumers needing deep history should run a
        Plus tier instance against the gateway.
        """
        # Pull a generous window from the exchange; cap at the protocol's
        # default 720 (Kraken's anonymous-tier limit). Larger limits land
        # with provider-side pagination work, not here.
        count = min(int(limit), 720)
        raw = self._exchange.get_ohlc(pair, interval=interval, count=count)

        rows = [_ohlc_candle_to_dict(c, pair, interval) for c in raw]

        # Client-side window filter — half-open [start, end), matching
        # ApiDataProvider/SqliteDataProvider semantics.
        if start is not None:
            start = _to_aware_utc(start)
            rows = [r for r in rows if r["datetime"] >= start]
        if end is not None:
            end = _to_aware_utc(end)
            rows = [r for r in rows if r["datetime"] < end]

        # Order: ExchangeProvider returns oldest-first; swap if 'desc'.
        if order == "desc":
            rows = list(reversed(rows))
        elif order != "asc":
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")

        # Final limit cap (after filters, in chosen order).
        if len(rows) > limit:
            rows = rows[:limit]

        return rows

    # ------------------------------------------------------------------
    # Pair registry
    # ------------------------------------------------------------------

    def get_pairs(self, instance: str | None = None) -> list[dict]:
        """Return active pairs from the bundled registry.

        The gateway response shape includes
        (pair, active, backfill_status, ohlc_from, instance, notes);
        the bundled JSON only carries (pair, active, instance, notes)
        because Free-tier Spirit has no central backfill table. Missing
        fields default to None on read so callers don't KeyError.
        """
        pairs = self._load_pairs()
        rows = [p for p in pairs if p.get("active", False)]
        if instance is not None:
            rows = [p for p in rows
                    if p.get("instance") is None or p.get("instance") == instance]
        return [
            {
                "pair": p["pair"],
                "active": p.get("active", True),
                "backfill_status": p.get("backfill_status"),
                "ohlc_from": p.get("ohlc_from"),
                "instance": p.get("instance"),
                "notes": p.get("notes"),
            }
            for p in rows
        ]

    def _load_pairs(self) -> list[dict]:
        if self._pairs_override is not None:
            return self._pairs_override
        if self._pairs_cache is not None:
            return self._pairs_cache
        with _PAIRS_PATH.open("r", encoding="utf-8") as f:
            blob = json.load(f)
        self._pairs_cache = list(blob.get("pairs", []))
        return self._pairs_cache


def _to_aware_utc(dt: datetime) -> datetime:
    """Coerce a datetime to tz-aware UTC. Naive is treated as UTC (Rule 11)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
