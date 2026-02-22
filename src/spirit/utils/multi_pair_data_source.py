"""
utils/multi_pair_data_source.py

Multi-pair data source layer — coordinates one data source per pair.

Live mode: one MultiIntervalLiveDataSource per pair (handles intervals internally).
CSV mode: splits a multi-pair CSV by pair, replays all pairs in lockstep.

Callbacks receive (pair: str, interval: int, window: OHLCData).
"""

from typing import Dict, List, Optional
import threading
import time

import pandas as pd

from spirit.logger import get_logger
from spirit.data_types import OHLCRecord, OHLCData
from spirit.config import (
    KRAKEN_OHLC_COUNT,
    KRAKEN_OHLC_BUFFER_DELAY_SECONDS,
)
from spirit.utils.kraken_ohlc_buffer import KrakenOHLCBuffer
from spirit.utils.multi_interval_data_source import MultiIntervalLiveDataSource

logger = get_logger("multi_pair_data_source")

__all__ = ["MultiPairLiveDataSource", "MultiPairCsvDataSource"]


class MultiPairLiveDataSource:
    """Manage one live data source per pair, emitting (pair, interval, window) callbacks."""

    def __init__(
        self,
        pairs: List[str],
        intervals: List[int],
        primary_interval: int,
        buffer_size: Optional[int] = None,
        buffer_delay_seconds: Optional[int] = None,
    ) -> None:
        self.pairs = list(pairs)
        self.intervals = sorted(int(i) for i in intervals)
        self.primary_interval = int(primary_interval)
        self.buffer_size = int(buffer_size if buffer_size is not None else KRAKEN_OHLC_COUNT)
        self.buffer_delay_seconds = (
            int(buffer_delay_seconds)
            if buffer_delay_seconds is not None
            else KRAKEN_OHLC_BUFFER_DELAY_SECONDS
        )

        self._callbacks = []  # cb(pair, interval, window)
        self.warmup_complete = False

        # One MultiIntervalLiveDataSource per pair
        self._sources: Dict[str, MultiIntervalLiveDataSource] = {}
        for pair in self.pairs:
            src = MultiIntervalLiveDataSource(
                intervals=self.intervals,
                primary_interval=self.primary_interval,
                buffer_size=self.buffer_size,
                pair=pair,
                buffer_delay_seconds=self.buffer_delay_seconds,
            )
            # Wire per-pair callback that prepends pair to the emission
            src.register_callback(self._make_pair_callback(pair))
            self._sources[pair] = src

        logger.info(
            f"[MultiPairLive] Initialized: pairs={self.pairs} "
            f"intervals={self.intervals} primary={self.primary_interval}"
        )

    def register_callback(self, func):
        """Register cb(pair: str, interval: int, window: OHLCData)."""
        self._callbacks.append(func)

    def is_monitoring_warm(self, pair: str, interval: int) -> bool:
        """Return True if the monitoring buffer for (pair, interval) has warmed up."""
        src = self._sources.get(pair)
        if src is None:
            return False
        return src.is_buffer_warm(int(interval))

    def wait_for_data(self, pair: str, interval: int, min_size: int, timeout: int = 180):
        """Block until the buffer for (pair, interval) has min_size candles."""
        if pair not in self._sources:
            raise ValueError(f"Pair {pair} not managed. Available: {list(self._sources.keys())}")
        self._sources[pair].wait_for_data(int(interval), min_size, timeout=timeout)

    def wait_for_all(self, interval: int, min_size: int, timeout: int = 180):
        """Block until ALL pairs have min_size candles for the given interval."""
        for pair in self.pairs:
            self.wait_for_data(pair, interval, min_size, timeout=timeout)

    def get_window(self, pair: str, interval: int, window_size: Optional[int] = None) -> OHLCData:
        """Get the current window for a specific (pair, interval)."""
        if pair not in self._sources:
            raise ValueError(f"Pair {pair} not managed. Available: {list(self._sources.keys())}")
        return self._sources[pair].get_window(int(interval), window_size)

    def stop(self):
        """Stop all per-pair data sources."""
        for pair, src in self._sources.items():
            try:
                src.stop()
            except Exception as e:
                logger.debug(f"[MultiPairLive] Error stopping {pair}: {e}")

    def _make_pair_callback(self, pair: str):
        """Create a closure that prepends pair to interval callbacks."""
        def _on_interval(interval: int, window: OHLCData):
            self._trigger_callbacks(pair, interval, window)
        return _on_interval

    def _trigger_callbacks(self, pair: str, interval: int, window: OHLCData):
        for cb in list(self._callbacks):
            try:
                cb(pair, interval, window)
            except Exception as e:
                logger.error(f"[MultiPairLive] Callback error pair={pair} interval={interval}: {e}")


class MultiPairCsvDataSource:
    """Multi-pair CSV replay data source.

    CSV must have columns: pair, interval, datetime, open, high, low, close, volume
    Optional: vwap, count, timestamp

    Replays candles in chronological order across all pairs.
    Callbacks receive (pair: str, interval: int, window: OHLCData).
    """

    def __init__(
        self,
        csv_path: str,
        pairs: List[str],
        primary_interval: int,
        intervals: Optional[List[int]] = None,
        window_size: int = KRAKEN_OHLC_COUNT,
        replay_delay: Optional[float] = None,
    ):
        self.csv_path = csv_path
        self.pairs = list(pairs)
        self.primary_interval = int(primary_interval)
        self.window_size = int(window_size)
        self.replay_delay = replay_delay

        self._callbacks = []
        self._stopped = False
        self.warmup_complete = False

        # Load CSV
        df = pd.read_csv(csv_path)
        required = {"pair", "interval", "datetime", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df["interval"] = df["interval"].astype(int)

        # Filter to requested pairs and intervals
        df = df[df["pair"].isin(self.pairs)]
        if intervals is not None:
            self.intervals = sorted(int(i) for i in intervals)
            df = df[df["interval"].isin(self.intervals)]
        else:
            self.intervals = sorted(df["interval"].unique().tolist())

        # Split by (pair, interval)
        self._data: Dict[str, Dict[int, pd.DataFrame]] = {}
        self._pointers: Dict[str, Dict[int, int]] = {}
        for pair in self.pairs:
            self._data[pair] = {}
            self._pointers[pair] = {}
            for itvl in self.intervals:
                pair_itvl_df = (
                    df[(df["pair"] == pair) & (df["interval"] == itvl)]
                    .sort_values("datetime")
                    .reset_index(drop=True)
                )
                self._data[pair][itvl] = pair_itvl_df
                # Start pointer after warmup window
                self._pointers[pair][itvl] = max(self.window_size - 1, 0)

    def register_callback(self, func):
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
        """CSV always has data ready. Returns True if enough rows exist."""
        df = self._data.get(pair, {}).get(int(interval))
        return (len(df) if df is not None else 0) >= int(min_size)

    def stop(self):
        self._stopped = True

    def __iter__(self):
        return self

    def __next__(self):
        """Advance one step across all pairs/intervals and emit callbacks."""
        if self._stopped:
            raise StopIteration

        progressed = False
        for pair in self.pairs:
            for itvl in self.intervals:
                df = self._data[pair][itvl]
                ptr = self._pointers[pair][itvl]
                next_ptr = ptr + 1
                if next_ptr < len(df):
                    self._pointers[pair][itvl] = next_ptr
                    start = max(0, next_ptr - self.window_size + 1)
                    window = self._build_window(df.iloc[start:next_ptr + 1], pair, itvl)
                    self._trigger_callbacks(pair, itvl, window)
                    progressed = True

        if not progressed:
            raise StopIteration
        if self.replay_delay:
            time.sleep(self.replay_delay)
        return True

    def _build_window(self, slice_df: pd.DataFrame, pair: str, interval: int) -> OHLCData:
        """Convert DataFrame slice to OHLCData."""
        records = [
            OHLCRecord.from_raw(
                pair=row.get("pair", pair),
                interval=int(interval),
                dt_raw=row["datetime"],
                open_=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                vwap=row.get("vwap", None),
                volume=row["volume"],
                count=row.get("count", None),
                timestamp=row.get("timestamp", None),
            )
            for _, row in slice_df.iterrows()
        ]
        return OHLCData(records=records)

    def _trigger_callbacks(self, pair: str, interval: int, window: OHLCData):
        for cb in list(self._callbacks):
            try:
                cb(pair, interval, window)
            except Exception as e:
                logger.error(f"[MultiPairCsv] Callback error pair={pair} interval={interval}: {e}")
