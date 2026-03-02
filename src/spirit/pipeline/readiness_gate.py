"""
DataReadinessGate: ensures downstream stages only evaluate when upstream
data is confirmed fresh.

Behaviour:
  - event_bus=None (Lite mode / replay / CSV): always returns True
  - event_bus set: waits for upstream event via LISTEN, with DB fallback
  - On timeout: logs warning, returns False (graceful degradation)
"""

from __future__ import annotations

from typing import Optional

from spirit.logger import get_logger
from spirit.pipeline.event_bus import PipelineEvent, PipelineEventBus
from spirit.pipeline.event_logger import check_event_exists

logger = get_logger("readiness_gate")


def _normalize_candle_dt(candle_dt: str) -> str:
    """Ensure candle_dt has +00:00 suffix for consistent string matching.

    OHLC daemons produce tz-aware strings ('2026-03-02T09:00:00+00:00')
    but Spirit context strips timezone ('2026-03-02T09:00:00'). The
    PgEventBus.wait_for() uses string equality, so both sides must match.
    """
    if candle_dt and not candle_dt.endswith('+00:00'):
        return candle_dt + '+00:00'
    return candle_dt


class DataReadinessGate:
    """Blocks until upstream pipeline stage confirms fresh data.

    Usage:
        gate = DataReadinessGate(event_bus=pg_event_bus, timeout=45)
        if gate.wait_for_dlimit(pair, candle_dt, interval=60):
            strategy.evaluate_trade(...)
        else:
            logger.warning("D-Limit not ready, evaluating with possibly stale data")
            strategy.evaluate_trade(...)  # graceful degradation
    """

    def __init__(
        self,
        event_bus: Optional[PipelineEventBus] = None,
        timeout: float = 45.0,
    ):
        self._event_bus = event_bus
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return self._event_bus is not None

    def wait_for_dlimit(
        self,
        pair: str,
        candle_dt: str,
        interval: int = 60,
    ) -> bool:
        """Wait for D-Limit processing to complete for this candle.

        Args:
            pair: Trading pair
            candle_dt: Candle datetime (ISO string)
            interval: D-Limit interval (60 or 15)

        Returns:
            True if event received (data is fresh), False on timeout
        """
        if self._event_bus is None:
            return True

        candle_dt = _normalize_candle_dt(candle_dt)
        stage = f"dlimit_{interval}m"
        channel = f"pipeline_{stage}"

        # Fallback: check if event already in DB (arrived before we subscribed)
        if check_event_exists(stage, pair, interval, candle_dt):
            logger.debug(
                f"[GATE] {pair} {interval}m {candle_dt} — already in pipeline_events"
            )
            return True

        # Wait for NOTIFY
        event = self._event_bus.wait_for(
            channel=channel,
            pair=pair,
            candle_dt=candle_dt,
            timeout=self._timeout,
        )

        if event is not None:
            logger.info(
                f"[GATE] {pair} {interval}m ready — "
                f"dur={event.duration_ms}ms rows={event.rows_affected}"
            )
            return True
        else:
            logger.warning(
                f"[GATE] {pair} {interval}m TIMEOUT after {self._timeout}s "
                f"for candle {candle_dt} — evaluating with possibly stale data"
            )
            return False

    def wait_for_ohlc(
        self,
        pair: str,
        candle_dt: str,
        interval: int = 60,
    ) -> bool:
        """Wait for OHLC data to be written for this candle.

        Primarily used by D-Limit daemons to confirm OHLC is available.
        """
        if self._event_bus is None:
            return True

        candle_dt = _normalize_candle_dt(candle_dt)
        channel = "pipeline_ohlc"

        if check_event_exists('ohlc', pair, interval, candle_dt):
            logger.debug(
                f"[GATE] OHLC {pair} {interval}m {candle_dt} — already in pipeline_events"
            )
            return True

        event = self._event_bus.wait_for(
            channel=channel,
            pair=pair,
            candle_dt=candle_dt,
            timeout=self._timeout,
        )

        if event is not None:
            logger.debug(
                f"[GATE] OHLC {pair} ready — dur={event.duration_ms}ms"
            )
            return True
        else:
            logger.warning(
                f"[GATE] OHLC {pair} {interval}m TIMEOUT for {candle_dt}"
            )
            return False
