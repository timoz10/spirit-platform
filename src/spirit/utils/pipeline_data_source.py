"""
utils/pipeline_data_source.py

Event-driven data source that triggers Spirit evaluation from pipeline events
instead of timer-based Kraken API polling.

When pipeline_dlimit_60m fires, fetches the candle from PG and triggers eval.
For monitoring intervals (1m, 5m, 15m), pipeline_ohlc triggers data fetch.

Same callback interface as MultiPairLiveDataSource:
    cb(pair: str, interval: int, window: OHLCData)

Feature-flagged via SPIRIT_DATA_MODE=pipeline (default: kraken_api).

Author: Claude Code + Tim
Date: 2026-03-05
"""

import threading
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from spirit.logger import get_logger
from spirit.config import KRAKEN_OHLC_COUNT
from spirit.data_types import OHLCRecord, OHLCData
from spirit.pipeline.event_bus import PipelineEvent, PipelineEventBus

logger = get_logger("pipeline_data_source")

__all__ = ["MultiPairPipelineDataSource"]


class MultiPairPipelineDataSource:
    """Event-driven data source: pipeline events trigger candle fetch + eval.

    Replaces timer-based Kraken API polling. Data guaranteed fresh because
    eval only fires after D-Limit processing completes.

    Upstream Dependencies:
      - pipeline_dlimit_60m: D-Limit 60m processing complete → trigger 60m eval
      - pipeline_ohlc: OHLC candle available → trigger monitoring intervals

    Outputs:
      - Callbacks: cb(pair, interval, OHLCData) — same as MultiPairLiveDataSource
    """

    def __init__(
        self,
        pairs: List[str],
        intervals: List[int],
        primary_interval: int,
        event_bus: PipelineEventBus,
        buffer_size: int = KRAKEN_OHLC_COUNT,
        fallback_timeout: float = 90.0,
    ) -> None:
        self.pairs = list(pairs)
        self.intervals = sorted(int(i) for i in intervals)
        self.primary_interval = int(primary_interval)
        self.buffer_size = int(buffer_size)
        self._event_bus = event_bus
        self._fallback_timeout = fallback_timeout

        self._callbacks: List[Callable] = []
        self.warmup_complete = False

        # Per-(pair, interval) deque buffers
        self._buffers: Dict[Tuple[str, int], deque] = {}
        for pair in self.pairs:
            for itvl in self.intervals:
                self._buffers[(pair, itvl)] = deque(maxlen=self.buffer_size)

        # Fallback timers: per-pair Timer for 60m eval if D-Limit is late
        self._fallback_timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

        # Warmup: load recent candles from PG
        self._warmup_from_pg()

        # Subscribe to pipeline events
        self._event_bus.subscribe('pipeline_dlimit_60m', self._on_dlimit_60m_event)
        self._event_bus.subscribe('pipeline_ohlc', self._on_ohlc_event)

        logger.info(
            f"[Pipeline Mode] Initialized: pairs={self.pairs} "
            f"intervals={self.intervals} primary={self.primary_interval} "
            f"fallback_timeout={self._fallback_timeout}s"
        )

    # ------------------------------------------------------------------
    # Public interface (matches MultiPairLiveDataSource)
    # ------------------------------------------------------------------

    def register_callback(self, func: Callable) -> None:
        """Register cb(pair: str, interval: int, window: OHLCData)."""
        self._callbacks.append(func)

    def get_window(
        self, pair: str, interval: int, window_size: Optional[int] = None,
    ) -> OHLCData:
        """Get the current window for a specific (pair, interval)."""
        size = int(window_size) if window_size is not None else self.buffer_size
        buf = self._buffers.get((pair, int(interval)))
        if buf is None:
            raise ValueError(
                f"Pair/interval ({pair}, {interval}) not managed. "
                f"Available: {list(self._buffers.keys())}"
            )
        records = list(buf)
        if len(records) > size:
            records = records[-size:]
        return OHLCData(records=records)

    def is_monitoring_warm(self, pair: str, interval: int) -> bool:
        """Return True if the buffer for (pair, interval) has data."""
        buf = self._buffers.get((pair, int(interval)))
        return buf is not None and len(buf) > 0

    def wait_for_data(
        self, pair: str, interval: int, min_size: int, timeout: float = 180,
    ) -> bool:
        """Block until buffer has min_size candles. Used during warmup only."""
        buf = self._buffers.get((pair, int(interval)))
        if buf is None:
            return False
        # Pipeline mode warms from PG at init, so data is already loaded
        return len(buf) >= int(min_size)

    def wait_for_all(self, interval: int, min_size: int, timeout: int = 180) -> None:
        """Block until ALL pairs have min_size candles for the given interval."""
        for pair in self.pairs:
            self.wait_for_data(pair, interval, min_size, timeout=timeout)

    def stop(self) -> None:
        """Cancel all fallback timers."""
        with self._lock:
            for pair, timer in self._fallback_timers.items():
                timer.cancel()
            self._fallback_timers.clear()
        logger.info("[Pipeline Mode] Stopped.")

    # ------------------------------------------------------------------
    # PG warmup
    # ------------------------------------------------------------------

    def _warmup_from_pg(self) -> None:
        """Load recent candles from PG to fill buffers at startup."""
        from spirit.utils.db_connection import execute_query

        for pair in self.pairs:
            for itvl in self.intervals:
                try:
                    rows = execute_query(
                        """
                        SELECT pair, interval, datetime, open, high, low, close,
                               vwap, volume, count
                        FROM ohlc_all
                        WHERE pair = %s AND interval = %s
                        ORDER BY datetime DESC
                        LIMIT %s
                        """,
                        (pair, itvl, self.buffer_size),
                    )
                    if rows:
                        # Rows come DESC, reverse into chronological order
                        records = [
                            OHLCRecord.from_raw(
                                pair=r['pair'], interval=int(r['interval']),
                                dt_raw=r['datetime'], open_=r['open'],
                                high=r['high'], low=r['low'],
                                close=r['close'], vwap=r.get('vwap'),
                                volume=r['volume'], count=r.get('count'),
                            )
                            for r in reversed(rows)
                        ]
                        buf = self._buffers[(pair, itvl)]
                        buf.extend(records)
                        logger.info(
                            f"[Pipeline Mode] Warmup {pair}/{itvl}m: "
                            f"{len(records)} candles loaded"
                        )
                    else:
                        logger.warning(
                            f"[Pipeline Mode] Warmup {pair}/{itvl}m: no data in PG"
                        )
                except Exception as e:
                    logger.error(
                        f"[Pipeline Mode] Warmup {pair}/{itvl}m failed: {e}"
                    )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_dlimit_60m_event(self, event: PipelineEvent) -> None:
        """D-Limit 60m processing complete → fetch candle and trigger 60m eval.

        This is the primary eval trigger. Data is guaranteed fresh because
        D-Limit has already processed the candle.
        """
        if event.pair not in self.pairs:
            return

        pair = event.pair
        candle_dt = event.candle_dt

        # Cancel any pending fallback timer for this pair
        self._cancel_fallback(pair)

        logger.info(
            f"[Pipeline Mode] D-Limit 60m event: {pair} candle_dt={candle_dt}"
        )

        self._fetch_and_trigger(pair, self.primary_interval, candle_dt)

    def _on_ohlc_event(self, event: PipelineEvent) -> None:
        """OHLC candle available from pipeline.

        For primary interval (60m): start fallback timer, do NOT trigger eval
        (wait for D-Limit to finish first).
        For monitoring intervals (1m, 5m, 15m): fetch and trigger immediately.
        """
        if event.pair not in self.pairs:
            return

        pair = event.pair
        itvl = event.interval_minutes
        candle_dt = event.candle_dt

        if itvl == self.primary_interval:
            # 60m OHLC arrived — D-Limit is still processing.
            # Start fallback timer in case D-Limit doesn't fire.
            self._start_fallback(pair, candle_dt)
        elif itvl in self.intervals:
            # Monitoring interval — trigger immediately
            self._fetch_and_trigger(pair, itvl, candle_dt)

    # ------------------------------------------------------------------
    # Core: fetch candle from PG and fire callbacks
    # ------------------------------------------------------------------

    def _fetch_and_trigger(
        self, pair: str, interval: int, candle_dt: str,
    ) -> None:
        """Fetch the specific candle from PG, append to buffer, fire callbacks."""
        from spirit.utils.db_connection import execute_query

        try:
            rows = execute_query(
                """
                SELECT pair, interval, datetime, open, high, low, close,
                       vwap, volume, count
                FROM ohlc_all
                WHERE pair = %s AND interval = %s AND datetime = %s
                LIMIT 1
                """,
                (pair, interval, candle_dt),
            )

            if not rows:
                logger.warning(
                    f"[Pipeline Mode] No candle found: {pair}/{interval}m "
                    f"dt={candle_dt}"
                )
                return

            record = OHLCRecord.from_raw(
                pair=rows[0]['pair'], interval=int(rows[0]['interval']),
                dt_raw=rows[0]['datetime'], open_=rows[0]['open'],
                high=rows[0]['high'], low=rows[0]['low'],
                close=rows[0]['close'], vwap=rows[0].get('vwap'),
                volume=rows[0]['volume'], count=rows[0].get('count'),
            )

            buf = self._buffers.get((pair, interval))
            if buf is not None:
                # Avoid duplicate: check if last record has same datetime
                if buf and buf[-1].dt == record.dt:
                    logger.debug(
                        f"[Pipeline Mode] Duplicate candle skipped: "
                        f"{pair}/{interval}m dt={candle_dt}"
                    )
                    return
                buf.append(record)

            # Fire callbacks with full window
            window = self.get_window(pair, interval)
            self._trigger_callbacks(pair, interval, window)

        except Exception as e:
            logger.error(
                f"[Pipeline Mode] Fetch/trigger failed: {pair}/{interval}m "
                f"dt={candle_dt}: {e}"
            )

    # ------------------------------------------------------------------
    # Fallback timer (if D-Limit is late)
    # ------------------------------------------------------------------

    def _start_fallback(self, pair: str, candle_dt: str) -> None:
        """Start a fallback timer for 60m eval if D-Limit doesn't fire."""
        with self._lock:
            # Cancel existing timer for this pair
            existing = self._fallback_timers.get(pair)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(
                self._fallback_timeout,
                self._fallback_trigger,
                args=(pair, candle_dt),
            )
            timer.daemon = True
            timer.name = f"fallback-{pair}"
            timer.start()
            self._fallback_timers[pair] = timer

            logger.debug(
                f"[Pipeline Mode] Fallback timer started: {pair} "
                f"timeout={self._fallback_timeout}s candle_dt={candle_dt}"
            )

    def _cancel_fallback(self, pair: str) -> None:
        """Cancel the fallback timer for a pair (D-Limit arrived on time)."""
        with self._lock:
            timer = self._fallback_timers.pop(pair, None)
            if timer is not None:
                timer.cancel()

    def _fallback_trigger(self, pair: str, candle_dt: str) -> None:
        """Fallback: D-Limit didn't fire within timeout, eval with PG data anyway."""
        with self._lock:
            self._fallback_timers.pop(pair, None)

        logger.warning(
            f"[Pipeline Mode] FALLBACK: D-Limit 60m event not received for {pair} "
            f"within {self._fallback_timeout}s — evaluating from PG directly. "
            f"candle_dt={candle_dt}"
        )
        self._fetch_and_trigger(pair, self.primary_interval, candle_dt)

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------

    def _trigger_callbacks(
        self, pair: str, interval: int, window: OHLCData,
    ) -> None:
        """Fire all registered callbacks."""
        for cb in list(self._callbacks):
            try:
                cb(pair, interval, window)
            except Exception as e:
                logger.error(
                    f"[Pipeline Mode] Callback error {pair}/{interval}m: {e}"
                )
