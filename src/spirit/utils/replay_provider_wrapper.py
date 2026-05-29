"""
utils/replay_provider_wrapper.py

Cursor-bounded, store-backed OHLC reads during ``--replay`` (ADR-0003).

``get_data_provider()`` wraps the tier provider in ``ReplayReadWrapper`` when a
replay run is active. The wrapper overrides only ``get_ohlc`` / ``get_user_ohlc``;
every other attribute falls through to the wrapped provider unchanged, so
consumers stay tier- and mode-agnostic (ADR-0001 — the DataProvider is the only
tier switch; this is a *mode* wrapper layered on top, not a tier branch).

Why this exists
---------------
Customer strategies read OHLC via ``dp.get_ohlc(pair, interval, limit=N)`` (see the
bundled ``sma_crossover`` example). Outside replay that means "latest N live
candles". During replay it must instead mean "latest N candles from the replay
store, as of the replay cursor" — i.e. replay data, with no look-ahead. Without
this wrapper the naive ``limit=N`` pattern silently reads live market data during a
backtest (#815 / #830), so the example strategy produces no meaningful trades.

We serve both tiers from their own store via ``get_user_ohlc`` — Free's local
SQLite ``user_ohlc`` and Plus's cloud ``/v1/ohlc/user`` (Plus has only
``read:ohlc_user``, not ``read:ohlc``, since migration 036). Both already support
``start``/``end``.

Author: Claude Code + Tim
Date: 2026-05-29
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

from spirit.logger import get_logger
from spirit.utils import replay_clock

logger = get_logger("replay_provider_wrapper")

__all__ = ["ReplayReadWrapper"]

# get_user_ohlc is half-open ``[start, end)``. Adding this epsilon to the cursor
# turns it into an upper bound that *includes* the cursor candle (candles are
# minute-aligned, so 1ms cannot pull in the next candle).
_CURSOR_EPS = timedelta(milliseconds=1)


def _to_utc(ts) -> pd.Timestamp:
    """Coerce a timestamp to tz-aware UTC so comparisons never mix naive/aware."""
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class ReplayReadWrapper:
    """Mode wrapper installing cursor-bounded store reads during replay."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    # ------------------------------------------------------------------
    # Cursor-bounded OHLC reads — both route to the store (get_user_ohlc)
    # ------------------------------------------------------------------

    def get_ohlc(self, pair, interval, *, start=None, end=None,
                 limit=5000, order="asc"):
        return self._bounded_read(
            pair, interval, start=start, end=end, limit=limit, order=order,
        )

    def get_user_ohlc(self, pair, interval, *, start=None, end=None,
                      limit=5000, order="asc"):
        return self._bounded_read(
            pair, interval, start=start, end=end, limit=limit, order=order,
        )

    def _bounded_read(self, pair, interval, *, start, end, limit, order):
        cursor = replay_clock.current()

        # Before the first candle is emitted (e.g. ReplayDataSource's own load),
        # there's no cursor to clamp to — pass through so the loader sees the
        # full requested range.
        if cursor is None:
            return self._inner.get_user_ohlc(
                pair, interval, start=start, end=end, limit=limit, order=order,
            )

        upper = _to_utc(cursor) + _CURSOR_EPS
        end_eff = upper if end is None else min(_to_utc(end), upper)

        if start is None:
            # "latest N as of the cursor" — the naive get_ohlc(limit=N) pattern.
            # Fetch newest-first then return ascending so the last row is the
            # latest candle, matching live get_ohlc(limit=N) semantics.
            rows = self._inner.get_user_ohlc(
                pair, interval, start=None, end=end_eff, limit=limit, order="desc",
            )
            rows = list(reversed(rows))
            if order == "desc":
                rows = list(reversed(rows))
            return rows

        return self._inner.get_user_ohlc(
            pair, interval, start=start, end=end_eff, limit=limit, order=order,
        )

    # ------------------------------------------------------------------
    # Everything else — delegated to the wrapped provider
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Only fires for attributes not found normally, so the two overrides
        # above take precedence and everything else lands here.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)
