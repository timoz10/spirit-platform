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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spirit.exchange.protocol import ExchangeProvider
from spirit.logger import get_logger

logger = get_logger("exchange_backed_data_provider")


_PAIRS_PATH = Path(__file__).resolve().parent.parent / "storage" / "pairs.json"

# Per-call chunk size for paged fetches. 720 is the common public-tier
# limit across the exchanges Spirit talks to today; if a future
# ExchangeProvider impl supports more (or less), expose this as a
# protocol property and read it through.
_CHUNK_SIZE = 720

# Safety cap on paged calls. 60_000 1m candles = ~84 chunks; doubling
# that leaves headroom without risking a runaway loop on a misbehaving
# exchange that always returns full pages.
_MAX_CHUNKS = 200


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
        page_sleep_s: float = 1.5,
    ) -> None:
        self._exchange = exchange
        # Lazy-loaded from the bundled JSON; explicit override accepted for
        # tests + advanced users running on alt exchanges.
        self._pairs_override = pairs
        self._pairs_cache: list[dict] | None = None
        # Inter-call pacing for paged fetches. Conservative default sits
        # under Kraken anon-tier's counter recharge (1/sec) without
        # tripping 429s on the OHLC endpoint (2-unit cost). Tests pass 0.
        self._page_sleep_s = page_sleep_s
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

        Two fetch strategies:
          - **No `start`**: single call to the exchange for the most-recent
            min(limit, _CHUNK_SIZE) candles. Used for warm-up + recent-history
            reads.
          - **`start` provided**: paged fetch from `start` forward, chunking
            _CHUNK_SIZE candles at a time, until `end` (if set), `limit`, or
            the exchange runs out of data. Used for deep-history backfills
            (e.g. trajectory recovery, BYOD OHLC sync — #666).

        Filtering and ordering happen client-side after stitching.
        """
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")

        start_aware = _to_aware_utc(start) if start is not None else None
        end_aware = _to_aware_utc(end) if end is not None else None

        if start_aware is None:
            raw = self._fetch_recent(pair, interval, limit)
        else:
            raw = self._fetch_paged(
                pair, interval, start_aware, end_aware, limit
            )

        rows = [_ohlc_candle_to_dict(c, pair, interval) for c in raw]

        # Client-side window filter — half-open [start, end), matching
        # ApiDataProvider/SqliteDataProvider semantics.
        if start_aware is not None:
            rows = [r for r in rows if r["datetime"] >= start_aware]
        if end_aware is not None:
            rows = [r for r in rows if r["datetime"] < end_aware]

        # Order: ExchangeProvider returns oldest-first; swap if 'desc'.
        if order == "desc":
            rows = list(reversed(rows))

        # Final limit cap (after filters, in chosen order).
        if len(rows) > limit:
            rows = rows[:limit]

        return rows

    # ------------------------------------------------------------------
    # Fetch strategies
    # ------------------------------------------------------------------

    def _fetch_recent(self, pair: str, interval: int, limit: int) -> list:
        """Single call for the most-recent min(limit, _CHUNK_SIZE) candles."""
        count = min(int(limit), _CHUNK_SIZE)
        return list(self._exchange.get_ohlc(
            pair, interval=interval, count=count
        ))

    def _fetch_paged(
        self,
        pair: str,
        interval: int,
        start: datetime,
        end: datetime | None,
        limit: int,
    ) -> list:
        """Page through `since`-cursored chunks from `start` forward.

        Stops at the first of:
          - chunk smaller than _CHUNK_SIZE (exchange has no more)
          - last candle timestamp >= end (if set)
          - collected >= limit (final trim happens upstream)
          - _MAX_CHUNKS calls (defensive — should never trigger in practice)

        Kraken's `since` is exclusive (timestamp > since), so the first
        chunk includes a candle exactly at `start` only if we cursor one
        second below it. Defensive timestamp-set dedup is applied in case
        a future ExchangeProvider impl is inclusive at the boundary.
        """
        cursor = int(start.timestamp()) - 1
        end_ts = int(end.timestamp()) if end is not None else None

        collected: list = []
        seen_ts: set[int] = set()

        for chunk_idx in range(_MAX_CHUNKS):
            chunk = list(self._exchange.get_ohlc(
                pair,
                interval=interval,
                count=_CHUNK_SIZE,
                since=cursor,
            ))
            if not chunk:
                break

            new_in_chunk = 0
            for candle in chunk:
                if candle.timestamp in seen_ts:
                    continue
                seen_ts.add(candle.timestamp)
                collected.append(candle)
                new_in_chunk += 1

            last_ts = chunk[-1].timestamp

            if len(chunk) < _CHUNK_SIZE:
                break
            if end_ts is not None and last_ts >= end_ts:
                break
            if len(collected) >= limit:
                break
            if new_in_chunk == 0:
                logger.warning(
                    f"[EXCHANGE] paged fetch made no progress at "
                    f"cursor={cursor}, breaking. pair={pair} interval={interval}"
                )
                break

            cursor = last_ts
            if self._page_sleep_s > 0:
                time.sleep(self._page_sleep_s)

        else:
            logger.warning(
                f"[EXCHANGE] paged fetch hit _MAX_CHUNKS ({_MAX_CHUNKS}) "
                f"for pair={pair} interval={interval} start={start.isoformat()} "
                f"— returning partial data ({len(collected)} candles)"
            )

        return collected

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
