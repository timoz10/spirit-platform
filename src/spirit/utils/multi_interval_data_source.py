"""
utils/multi_interval_data_source.py

Manage multiple live OHLC buffers and emit per-interval windows.
Callbacks will be invoked as cb(interval, window_df), where interval is
an int (minutes) and window_df is an OHLCData instance.
"""

from typing import Dict, List, Optional
import threading

from spirit.logger import logger
from spirit.data_types import OHLCData
from spirit.config import (
    KRAKEN_OHLC_COUNT,
    KRAKEN_PAIR,
    KRAKEN_OHLC_BUFFER_DELAY_SECONDS,
)
from spirit.utils.kraken_ohlc_buffer import KrakenOHLCBuffer

__all__ = ["MultiIntervalLiveDataSource"]


class MultiIntervalLiveDataSource:
    """Manage multiple live OHLC buffers and emit per-interval windows."""

    def __init__(
        self,
        intervals: List[int],
        primary_interval: int,
        buffer_size: Optional[int] = None,
        pair: str = KRAKEN_PAIR,
        buffer_delay_seconds: Optional[int] = None,
    ) -> None:
        self.intervals = sorted(int(i) for i in intervals)
        self.primary_interval = int(primary_interval)
        self.buffer_size = int(buffer_size if buffer_size is not None else KRAKEN_OHLC_COUNT)
        self.pair = pair
        self.buffer_delay_seconds = (
            int(buffer_delay_seconds)
            if buffer_delay_seconds is not None
            else KRAKEN_OHLC_BUFFER_DELAY_SECONDS
        )

        self._callbacks = []  # functions of form cb(interval, window_df)
        self.buffers: Dict[int, KrakenOHLCBuffer] = {}
        self.warmup_complete = False

        # Spin up a KrakenOHLCBuffer per interval and hook its callback
        for itvl in self.intervals:
            buf = KrakenOHLCBuffer(
                buffer_size=self.buffer_size,
                interval=int(itvl),
                buffer_delay_seconds=self.buffer_delay_seconds,
                pair=self.pair,
            )
            buf.register_callback(self._make_interval_callback(int(itvl)))
            buf.start_background_updater()
            self.buffers[int(itvl)] = buf

    def register_callback(self, func):
        self._callbacks.append(func)

    def wait_for_data(self, interval: int, min_size: int, timeout: int = 60):
        interval = int(interval)
        if interval not in self.buffers:
            raise ValueError(f"Interval {interval} not managed by this data source.")
        logger.debug(
            f"[MultiInterval] wait_for_data: interval={interval}, min_size={min_size}, timeout={timeout}"
        )
        self.buffers[interval].wait_for_buffer(min_size, timeout=timeout)

    def get_window(self, interval: int, window_size: Optional[int] = None) -> OHLCData:
        interval = int(interval)
        if interval not in self.buffers:
            raise ValueError(f"Interval {interval} not managed by this data source.")
        if window_size is None:
            window_size = self.buffer_size
        buf = self.buffers[interval]
        thread_id = threading.get_ident()
        logger.debug(
            f"[MultiInterval.get_window] ENTER thread={thread_id} interval={interval} window_size={window_size}"
        )
        try:
            with buf.lock:
                candles = list(buf.buffer)[-int(window_size) :]
        except Exception as e:
            logger.error(f"[MultiInterval.get_window] Exception inside buffer.lock for interval={interval}: {e}")
            raise
        logger.debug(
            f"[MultiInterval.get_window] EXIT thread={thread_id} interval={interval} returning {len(candles)} candles."
        )
        return OHLCData(records=candles)

    def stop(self):
        for itvl, buf in self.buffers.items():
            try:
                buf.stop()
            except Exception as e:
                logger.debug(f"[MultiInterval.stop] Error stopping interval={itvl}: {e}")

    def _trigger_callbacks(self, interval: int, window: OHLCData):
        for cb in list(self._callbacks):
            try:
                cb(interval, window)
            except Exception as e:
                logger.error(f"[MultiInterval] Exception in callback for interval={interval}: {e}")

    def _make_interval_callback(self, interval: int):
        def _on_new_candle(_candle):
            try:
                window = self.get_window(interval, self.buffer_size)
                self._trigger_callbacks(interval, window)
            except Exception as e:
                logger.error(f"[MultiInterval] _on_new_candle error interval={interval}: {e}")

        return _on_new_candle
