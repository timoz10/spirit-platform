"""
utils/replay_data_source.py

Store-backed replay data source for historical backtesting through the full
Spirit pipeline.

Same interface as MultiPairCsvDataSource — drop-in replacement that reads OHLC
from the runtime DataProvider's store (Free → local SQLite ``user_ohlc``;
Plus → cloud ``/v1/ohlc/user``) via ``get_user_ohlc``, rather than from raw
PostgreSQL. The former PG-only path crashed on the framework wheel because
``spirit.utils.db_connection`` is deliberately not bundled (#830).

Loads all requested (pair, interval) data into memory at init, then replays
candles chronologically with callbacks. Strategy-side OHLC reads during the
run are cursor-bounded by ReplayReadWrapper (ADR-0003).

Author: Claude Code + Tim
Date: 2026-02-18 (PG); store-backed rewrite 2026-05-29 (#830)
"""

import time
from typing import Dict, List, Optional, Callable
from datetime import datetime

import pandas as pd

from spirit.logger import get_logger
from spirit.config import KRAKEN_OHLC_COUNT
from spirit.data_types import OHLCRecord, OHLCData
from spirit.utils import replay_clock

logger = get_logger("replay_data_source")

__all__ = ["ReplayDataSource"]

# Upper bound on rows loaded per (pair, interval) for a replay window. Matches
# the gateway's max page size; comfortably covers realistic windows (a 90-day
# 1m backtest is ~129.6k rows). Larger windows log a truncation warning.
_REPLAY_LOAD_LIMIT = 200_000


class ReplayDataSource:
    """Multi-pair PG replay data source.

    Queries ohlc_all once at init, loads into memory, then replays candles
    in chronological order across all pairs/intervals.

    Callbacks receive (pair: str, interval: int, window: OHLCData).
    """

    def __init__(
        self,
        pairs: List[str],
        primary_interval: int,
        start_date: str,
        end_date: str,
        intervals: Optional[List[int]] = None,
        window_size: int = KRAKEN_OHLC_COUNT,
    ):
        self.pairs = list(pairs)
        self.primary_interval = int(primary_interval)
        self.start_date = start_date
        self.end_date = end_date
        self.window_size = int(window_size)

        self._callbacks: List[Callable] = []
        self._stopped = False
        self.warmup_complete = False
        self._replay_start_ts = pd.Timestamp(start_date, tz='UTC')

        # Determine intervals
        if intervals is not None:
            self.intervals = sorted(int(i) for i in intervals)
        else:
            self.intervals = [self.primary_interval]

        # Load data from PG
        self._data: Dict[str, Dict[int, pd.DataFrame]] = {}
        self._pointers: Dict[str, Dict[int, int]] = {}
        self._load_candles()

        total_candles = sum(
            len(df) for pair_data in self._data.values() for df in pair_data.values()
        )
        logger.info(
            f"[Replay] Loaded {total_candles} candles: "
            f"pairs={self.pairs} intervals={self.intervals} "
            f"range={self.start_date} to {self.end_date}"
        )

    def _load_candles(self):
        """Load each (pair, interval) range from the runtime DataProvider store
        and split into per-(pair, interval) DataFrames.

        Replaces the former PG-only ``_load_from_pg`` (#830): ``db_connection``
        is intentionally stripped from the framework wheel, so replay reads
        through the tier DataProvider like every other consumer (ADR-0001) —
        Free → local SQLite ``user_ohlc``; Plus → cloud ``/v1/ohlc/user`` (the
        only OHLC a Plus key is entitled to since migration 036). Loads extra
        data before start_date to fill the warmup window, so the first replay
        callback corresponds to a candle at ~start_date.

        This load runs before any candle is emitted, so the replay clock cursor
        is still ``None`` and the ReplayReadWrapper passes the full requested
        range through unclamped (see replay_clock.current()).
        """
        from datetime import timedelta
        from spirit.utils.data_provider import get_data_provider

        dp = get_data_provider()

        # Compute warmup lookback based on primary interval only.
        # With monitoring intervals (e.g. 1m + 60m), using max_interval
        # would load 720*60=43200 minutes of 1m warmup candles needlessly.
        warmup_minutes = self.window_size * self.primary_interval
        warmup_start = (
            pd.Timestamp(self.start_date, tz='UTC') - timedelta(minutes=warmup_minutes)
        ).to_pydatetime()
        # get_user_ohlc is half-open [start, end); widen by one primary
        # interval so the candle at exactly end_date is still included,
        # preserving the old ``datetime <= end_date`` inclusivity.
        end_excl = (
            pd.Timestamp(self.end_date, tz='UTC') + timedelta(minutes=self.primary_interval)
        ).to_pydatetime()

        all_rows: list[dict] = []
        for pair in self.pairs:
            for itvl in self.intervals:
                rows = dp.get_user_ohlc(
                    pair, itvl,
                    start=warmup_start, end=end_excl,
                    limit=_REPLAY_LOAD_LIMIT, order='asc',
                )
                if len(rows) >= _REPLAY_LOAD_LIMIT:
                    logger.warning(
                        "[Replay] %s/%dm hit the %d-row load cap — the replay "
                        "window may be truncated. Narrow --start/--end or split "
                        "the run.",
                        pair, itvl, _REPLAY_LOAD_LIMIT,
                    )
                all_rows.extend(dict(r) for r in rows)

        if not all_rows:
            logger.warning("[Replay] No OHLC data returned from the store")
            for pair in self.pairs:
                self._data[pair] = {}
                self._pointers[pair] = {}
                for itvl in self.intervals:
                    self._data[pair][itvl] = pd.DataFrame()
                    self._pointers[pair][itvl] = 0
            return

        df = pd.DataFrame(all_rows)
        df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
        df['interval'] = df['interval'].astype(int)

        for pair in self.pairs:
            self._data[pair] = {}
            self._pointers[pair] = {}
            for itvl in self.intervals:
                pair_itvl_df = (
                    df[(df['pair'] == pair) & (df['interval'] == itvl)]
                    .sort_values('datetime')
                    .reset_index(drop=True)
                )
                self._data[pair][itvl] = pair_itvl_df
                # Start pointer: primary interval uses full warmup window,
                # monitoring intervals skip to first candle at/after start_date
                if itvl == self.primary_interval:
                    self._pointers[pair][itvl] = max(self.window_size - 1, 0)
                else:
                    if not pair_itvl_df.empty:
                        idx = pair_itvl_df['datetime'].searchsorted(self._replay_start_ts)
                        self._pointers[pair][itvl] = max(int(idx) - 1, 0)
                    else:
                        self._pointers[pair][itvl] = 0

            pair_total = sum(len(self._data[pair][i]) for i in self.intervals)
            logger.info(f"[Replay] {pair}: {pair_total} candles across {len(self.intervals)} intervals")

    def register_callback(self, func: Callable):
        """Register cb(pair: str, interval: int, window: OHLCData)."""
        self._callbacks.append(func)

    def get_window(self, pair: str, interval: int, window_size: Optional[int] = None) -> OHLCData:
        """Get current window for a specific (pair, interval)."""
        size = int(window_size if window_size is not None else self.window_size)
        df = self._data.get(pair, {}).get(int(interval))
        if df is None or df.empty:
            return OHLCData(records=[])
        ptr = self._pointers[pair][int(interval)]
        end = min(ptr + 1, len(df))
        start = max(0, end - size)
        return self._build_window(df.iloc[start:end], pair, int(interval))

    def wait_for_data(self, pair: str, interval: int, min_size: int, timeout: float = None) -> bool:
        """Replay always has data ready. Returns True if enough rows exist."""
        df = self._data.get(pair, {}).get(int(interval))
        return (len(df) if df is not None else 0) >= int(min_size)

    def wait_for_all(self, interval: int, min_size: int, timeout: int = 180):
        """Block until ALL pairs have min_size candles for the given interval."""
        for pair in self.pairs:
            self.wait_for_data(pair, interval, min_size, timeout=timeout)

    def stop(self):
        self._stopped = True

    def __iter__(self):
        return self

    def __next__(self):
        """Advance to the next candle in chronological order and emit callback.

        Each step finds the (pair, interval) with the earliest next candle
        timestamp and advances only that pointer. This ensures 1m and 60m
        candles interleave correctly by time (e.g., one 60m callback fires
        for every 60 1m callbacks).

        Tie-break: when multiple (pair, interval) share the same timestamp,
        the candidate is selected by tuple comparison (ts, pair, interval).
        This pins eval order to alphabetical pair → ascending interval
        regardless of `self.pairs` iteration order, so two replay runs with
        the same data emit candles in byte-identical sequence (#523). The
        prior `ts < best[0]` strict comparator silently kept whichever
        pair came first in iteration order, leaving the eval order at the
        mercy of any caller change to pair list construction.

        Candles before start_date are silently skipped (they exist for warmup).
        """
        if self._stopped:
            raise StopIteration

        while True:
            # Find the (pair, interval) with the earliest next candle.
            # Tuple comparison gives deterministic tie-breaks (#523).
            best = None  # (timestamp, pair, interval)
            for pair in self.pairs:
                for itvl in self.intervals:
                    df = self._data[pair][itvl]
                    next_ptr = self._pointers[pair][itvl] + 1
                    if next_ptr < len(df):
                        ts = df.iloc[next_ptr]['datetime']
                        candidate = (ts, pair, itvl)
                        if best is None or candidate < best:
                            best = candidate

            if best is None:
                raise StopIteration

            ts, pair, itvl = best
            next_ptr = self._pointers[pair][itvl] + 1
            self._pointers[pair][itvl] = next_ptr

            # Skip warmup candles (before replay start date)
            if ts < self._replay_start_ts:
                continue

            df = self._data[pair][itvl]
            start = max(0, next_ptr - self.window_size + 1)
            window = self._build_window(df.iloc[start:next_ptr + 1], pair, itvl)
            # Publish the replay cursor BEFORE firing callbacks so any
            # dp.get_ohlc(...) inside strategy evaluation is bounded to this
            # candle's time (no look-ahead). Monotonic — see replay_clock.
            replay_clock.advance_to(ts)
            self._trigger_callbacks(pair, itvl, window)
            return True

    def _build_window(self, slice_df: pd.DataFrame, pair: str, interval: int) -> OHLCData:
        """Convert DataFrame slice to OHLCData."""
        records = [
            OHLCRecord.from_raw(
                pair=row.get('pair', pair),
                interval=int(interval),
                dt_raw=row['datetime'],
                open_=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                vwap=row.get('vwap', None),
                volume=row['volume'],
                count=row.get('count', None),
                timestamp=row.get('timestamp', None),
            )
            for _, row in slice_df.iterrows()
        ]
        return OHLCData(records=records)

    def _trigger_callbacks(self, pair: str, interval: int, window: OHLCData):
        for cb in list(self._callbacks):
            try:
                cb(pair, interval, window)
            except Exception as e:
                logger.error(f"[Replay] Callback error pair={pair} interval={interval}: {e}")
