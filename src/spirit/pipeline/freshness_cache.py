"""
PipelineFreshnessCache — push-updated freshness tracker for pipeline stages.

Replaces the blocking ``DataReadinessGate.wait_for_*`` pattern. Spirit's tick
loop makes non-blocking ``is_fresh()`` checks; the cache is updated out-of-band
when pipeline events arrive on the WsEventBus.

Semantics:

  - No timeout, no blocking. A tick either has fresh data or it doesn't.
  - On miss, callers skip the work and log ``[MISS]`` — next tick re-checks.
    No "stale fallback" — we never trade on silently-old indicators.
  - Cache entries carry the ``candle_dt`` of the most recent pipeline event
    per ``(pair, stage, interval_minutes)``. ``is_fresh(need_candle_dt)``
    returns True iff cached candle_dt >= requested.
  - Thread-safe: updates fire from the WsEventBus dispatch thread while
    Spirit's tick loop reads. Uses a coarse ``threading.Lock`` around a
    plain dict — volume is low (handfuls of events per minute) so we don't
    need anything fancier.

Typical wiring in Spirit's main:

  cache = PipelineFreshnessCache()
  event_bus.subscribe('pipeline_dlimit_60m', cache.record)
  event_bus.subscribe('pipeline_dlimit_15m', cache.record)
  # ... then
  if not cache.is_fresh('XBTUSD', 'dlimit_60m', 60, expected_candle_dt):
      logger.info("[MISS] dlimit_60m stale — skip entry eval this tick")
      return
  # proceed with evaluation
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from spirit.logger import get_logger
from spirit.pipeline.event_bus import PipelineEvent

logger = get_logger("freshness_cache")


# Stages Spirit actually gates on. Others are accepted but not gated —
# we still record them so introspection (diagnostics, /status) can show
# the full picture.
_GATED_STAGES = frozenset({
    "dlimit_60m",
    "dlimit_15m",
    "bounce_physics",
})


def _normalize_candle_dt(candle_dt: Optional[str]) -> Optional[str]:
    """Match the convention used by DataReadinessGate: tz-aware ISO strings
    end with ``+00:00``. Spirit context sometimes strips the tz suffix; the
    gateway and daemons emit with ``+00:00``. Align both sides so string
    comparison works deterministically.
    """
    if not candle_dt:
        return None
    if candle_dt.endswith("Z"):
        return candle_dt[:-1] + "+00:00"
    if len(candle_dt) >= 19 and "+" not in candle_dt[10:] and "-" not in candle_dt[11:]:
        return candle_dt + "+00:00"
    return candle_dt


class PipelineFreshnessCache:
    """Push-updated per-(pair, stage, interval) freshness tracker.

    Non-blocking ``is_fresh()`` replaces the old
    ``DataReadinessGate.wait_for_dlimit(timeout=N)`` pattern.
    """

    def __init__(self) -> None:
        # (pair, stage, interval_minutes) -> normalized candle_dt ISO
        self._latest: Dict[Tuple[str, str, int], str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Updates (push path)
    # ------------------------------------------------------------------

    def record(self, event: PipelineEvent) -> None:
        """Record the candle_dt of a completed pipeline stage.

        Called from WsEventBus dispatch thread. Monotonic: never moves
        a cache entry backward.
        """
        candle = _normalize_candle_dt(event.candle_dt)
        if candle is None:
            return
        key = (event.pair, event.stage, event.interval_minutes)
        with self._lock:
            prev = self._latest.get(key)
            if prev is None or candle > prev:
                self._latest[key] = candle
                logger.debug(
                    "[FRESHNESS] %s %s %dm -> %s",
                    event.pair, event.stage, event.interval_minutes, candle,
                )

    def seed(
        self, pair: str, stage: str, interval_minutes: int, candle_dt: str
    ) -> None:
        """Manually seed a cache entry. Use after warmup to prime the cache
        with the candle_dt of the latest data pulled via REST — avoids
        spurious ``[MISS]`` on the first few ticks before WS events roll in.
        """
        candle = _normalize_candle_dt(candle_dt)
        if candle is None:
            return
        key = (pair, stage, interval_minutes)
        with self._lock:
            prev = self._latest.get(key)
            if prev is None or candle > prev:
                self._latest[key] = candle
                logger.debug(
                    "[FRESHNESS] seed %s %s %dm -> %s",
                    pair, stage, interval_minutes, candle,
                )

    # ------------------------------------------------------------------
    # Queries (pull path — must be non-blocking)
    # ------------------------------------------------------------------

    def is_fresh(
        self,
        pair: str,
        stage: str,
        interval_minutes: int,
        need_candle_dt: str,
    ) -> bool:
        """True iff the cache has seen ``stage`` completion for ``pair``
        at ``interval_minutes`` with ``candle_dt >= need_candle_dt``.

        Non-blocking. Returns immediately — no timeout, no wait.
        """
        need = _normalize_candle_dt(need_candle_dt)
        if need is None:
            return False
        with self._lock:
            latest = self._latest.get((pair, stage, interval_minutes))
        return latest is not None and latest >= need

    def latest(
        self, pair: str, stage: str, interval_minutes: int
    ) -> Optional[str]:
        """Return the cached candle_dt for ``(pair, stage, interval)`` or None."""
        with self._lock:
            return self._latest.get((pair, stage, interval_minutes))

    def snapshot(self) -> Dict[Tuple[str, str, int], str]:
        """Return a shallow copy of the cache for diagnostics."""
        with self._lock:
            return dict(self._latest)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @staticmethod
    def is_gated_stage(stage: str) -> bool:
        """Whether ``stage`` is one Spirit actively gates on.

        Other stages may be recorded in the cache for diagnostic purposes
        but don't block evaluation.
        """
        return stage in _GATED_STAGES
