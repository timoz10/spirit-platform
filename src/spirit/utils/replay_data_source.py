"""
utils/replay_data_source.py

PG-backed replay data source for historical backtesting through the full Spirit pipeline.

Same interface as MultiPairCsvDataSource — drop-in replacement that reads OHLC
from PostgreSQL ohlc_all instead of a CSV file.

Loads all requested (pair, interval) data into memory at init, then replays
candles chronologically with callbacks.

Author: Claude Code + Tim
Date: 2026-02-18
"""

import time
from typing import Dict, List, Optional, Callable
from datetime import datetime

import pandas as pd

from spirit.logger import get_logger
from spirit.config import KRAKEN_OHLC_COUNT
from spirit.data_types import OHLCRecord, OHLCData

logger = get_logger("replay_data_source")

__all__ = ["ReplayDataSource"]


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
        self._load_from_pg()

        total_candles = sum(
            len(df) for pair_data in self._data.values() for df in pair_data.values()
        )
        logger.info(
            f"[Replay] Loaded {total_candles} candles: "
            f"pairs={self.pairs} intervals={self.intervals} "
            f"range={self.start_date} to {self.end_date}"
        )

    def _load_from_pg(self):
        """Query ohlc_all and split into per-(pair, interval) DataFrames.

        Loads extra data before start_date to fill the warmup window, so that
        the first replay callback corresponds to a candle at ~start_date.
        """
        from spirit.utils.db_connection import execute_query
        from datetime import timedelta

        pair_placeholders = ','.join(['%s'] * len(self.pairs))
        interval_placeholders = ','.join(['%s'] * len(self.intervals))

        # Compute warmup lookback: window_size * max_interval minutes
        max_interval_min = max(self.intervals)
        warmup_minutes = self.window_size * max_interval_min
        warmup_start = (
            pd.Timestamp(self.start_date) - timedelta(minutes=warmup_minutes)
        ).strftime('%Y-%m-%d %H:%M:%S')

        query = f"""
            SELECT pair, interval, datetime, open, high, low, close,
                   vwap, volume, count
            FROM ohlc_all
            WHERE pair IN ({pair_placeholders})
              AND interval IN ({interval_placeholders})
              AND datetime >= %s AND datetime <= %s
            ORDER BY datetime
        """
        params = tuple(self.pairs) + tuple(self.intervals) + (warmup_start, self.end_date)
        rows = execute_query(query, params)

        if not rows:
            logger.warning("[Replay] No OHLC data returned from PG")
            for pair in self.pairs:
                self._data[pair] = {}
                self._pointers[pair] = {}
                for itvl in self.intervals:
                    self._data[pair][itvl] = pd.DataFrame()
                    self._pointers[pair][itvl] = 0
            return

        df = pd.DataFrame([dict(r) for r in rows])
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
                # Start pointer after warmup window
                self._pointers[pair][itvl] = max(self.window_size - 1, 0)

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

        Candles before start_date are silently skipped (they exist for warmup).
        """
        if self._stopped:
            raise StopIteration

        while True:
            # Find the (pair, interval) with the earliest next candle
            best = None  # (timestamp, pair, interval)
            for pair in self.pairs:
                for itvl in self.intervals:
                    df = self._data[pair][itvl]
                    next_ptr = self._pointers[pair][itvl] + 1
                    if next_ptr < len(df):
                        ts = df.iloc[next_ptr]['datetime']
                        if best is None or ts < best[0]:
                            best = (ts, pair, itvl)

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
