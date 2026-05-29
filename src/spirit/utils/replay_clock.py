"""
utils/replay_clock.py

Single source of "current replay time" for cursor-bounded OHLC reads (ADR-0003).

During ``--replay`` the candle data lives in ``ReplayDataSource``, but strategies
read OHLC through ``get_data_provider().get_ohlc(...)`` (see the bundled
``sma_crossover`` example — the canonical customer pattern). To stop those reads
seeing future data (look-ahead) and to make them return *replay* data rather than
*live* market data, ``get_data_provider()`` installs ``ReplayReadWrapper``, which
clamps every read's upper bound to the timestamp of the most-recently-emitted
replay candle. That timestamp is published here.

Replay is strictly single-threaded — ``ReplayDataSource.__next__`` fires callbacks
synchronously and per-pair evaluation is serialized under ``_eval_locks`` — so a
plain module-global scalar is sufficient. No locking is required. See ADR-0003.

Author: Claude Code + Tim
Date: 2026-05-29
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

__all__ = ["activate", "is_active", "advance_to", "current", "reset"]

_active: bool = False
_cursor: Optional[pd.Timestamp] = None


def activate() -> None:
    """Mark a replay run as active so ``get_data_provider()`` installs the
    cursor-bounded read wrapper. Resets any prior cursor. Idempotent."""
    global _active, _cursor
    _active = True
    _cursor = None


def is_active() -> bool:
    """True between ``activate()`` and ``reset()`` — i.e. during a replay run."""
    return _active


def advance_to(ts) -> None:
    """Advance the replay cursor to the latest emitted candle time (monotonic).

    Called by ``ReplayDataSource.__next__`` for every emitted candle. The cursor
    only ever moves forward, so interleaved 1m/60m streams never let a later 1m
    read regress the bound below a 60m candle already emitted.
    """
    global _cursor
    if ts is None:
        return
    ts = pd.Timestamp(ts)
    if _cursor is None or ts > _cursor:
        _cursor = ts


def current() -> Optional[pd.Timestamp]:
    """The current replay time, or ``None`` before the first candle is emitted.

    ``None`` deliberately means "no clamp yet" so ``ReplayDataSource``'s own
    load (which runs before any candle is emitted) reads the full requested
    range through the wrapper unclamped.
    """
    return _cursor


def reset() -> None:
    """Test-only: clear all replay-clock state."""
    global _active, _cursor
    _active = False
    _cursor = None
