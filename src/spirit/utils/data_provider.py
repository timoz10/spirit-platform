"""
DataProvider — Abstract data access for Spirit runtime.

Spirit is api-driven: all data access goes through the API gateway.
pg-mode was removed in the platform pivot (#340).

Framework / IP boundary (#340):
  - FrameworkDataProvider: data any Spirit instance needs (OHLC, state,
    performance, heartbeats, pairs). Used by both free-tier and
    subscription-tier Spirit.
  - IPDataProvider: data produced by our IP pipeline (D-Limit zones,
    bounces, regime-derived indicators, risk gate, theses, scorer,
    tumblers, trajectories). Available only to subscription-tier Spirit.
  - DataProvider: union of both, satisfied by the subscription-tier
    ApiDataProvider. Free-tier Spirit will implement FrameworkDataProvider
    only (backed by local SQLite — future work).

Usage:
    from spirit.utils.data_provider import get_data_provider
    dp = get_data_provider()
    rows = dp.get_ohlc(pair='XBTUSD', interval=60, limit=100)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from spirit.logger import get_logger

logger = get_logger("data_provider")


# =====================================================================
# Framework interface — data any Spirit instance needs
# =====================================================================


@runtime_checkable
class FrameworkDataProvider(Protocol):
    """Framework data access: OHLC, state, performance, heartbeats, pairs.

    Any Spirit instance — free-tier or subscription — uses this interface.
    Free-tier implementations back it with local SQLite; subscription-tier
    backs it with the API gateway.
    """

    # -----------------------------------------------------------------
    # OHLC
    # -----------------------------------------------------------------

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
        """Fetch OHLC candles. Returns list of dicts with keys:
        pair, interval, datetime, open, high, low, close, vwap, volume, count.
        """
        ...

    # -----------------------------------------------------------------
    # State (spirit_state key-value store) — crash recovery
    # -----------------------------------------------------------------

    def get_state(self, key: str) -> Any:
        """Fetch a single value from spirit_state by exact key."""
        ...

    def put_state(self, key: str, value: Any) -> None:
        """Upsert a key-value pair into spirit_state."""
        ...

    def ensure_table(self, table_name: str, create_sql: str) -> bool:
        """Ensure a table exists. API-backed: no-op (True)."""
        ...

    # -----------------------------------------------------------------
    # Performance — the user's own trade outcomes
    # -----------------------------------------------------------------

    def get_performance(
        self,
        *,
        pair: str | None = None,
        strategy: str | None = None,
        source: str | None = None,
        run_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch strategy_performance rows."""
        ...

    def write_performance(self, data: dict) -> int:
        """Insert a single trade result. Returns rows affected."""
        ...

    def write_performance_batch(self, trades: list[dict]) -> int:
        """Bulk insert trade results. Returns rows affected."""
        ...

    def clear_performance(self, *, run_id: str) -> int:
        """Delete performance rows for a specific run_id. Returns rows deleted."""
        ...

    def get_strategy_metrics(
        self,
        strategy_name: str,
        as_of_date: datetime,
        *,
        baseline_days: int = 90,
        current_days: int = 14,
        recent_days: int = 7,
    ) -> dict:
        """Per-strategy win-rate + streak metrics from strategy_performance.

        Pure trade-outcome aggregation — framework data (users own their
        trades). Consumed by IP regime engine but the aggregation itself
        is not IP-derived.
        """
        ...

    # -----------------------------------------------------------------
    # Heartbeats — daemon health
    # -----------------------------------------------------------------

    def write_heartbeat(
        self,
        daemon_id: str,
        *,
        instance: str,
        status: str = "ok",
        metadata: dict | None = None,
        run_id: str = "live",
    ) -> int:
        """Upsert a daemon heartbeat. Returns rows affected."""
        ...

    # -----------------------------------------------------------------
    # Pairs — which symbols are active for this instance
    # -----------------------------------------------------------------

    def get_pairs(self, instance: str | None = None) -> list[dict]:
        """Fetch active pairs from pair registry."""
        ...


# =====================================================================
# IP interface — data produced by our IP pipeline
# =====================================================================


@runtime_checkable
class IPDataProvider(Protocol):
    """IP data access: zones, bounces, D-Limit, regime, calibrations.

    Subscription-tier only. Free-tier Spirit does not consume this
    interface — users bring their own algorithms and data feeds.
    """

    # -----------------------------------------------------------------
    # D-Limit zones + touches + bounces
    # -----------------------------------------------------------------

    def get_zones(
        self,
        pair: str,
        interval: int = 60,
        *,
        active: bool | None = None,
        min_strength: float | None = None,
    ) -> list[dict]:
        """Fetch D-Limit zones."""
        ...

    def get_zone_touches(
        self,
        pair: str,
        *,
        zone_ids: list[int] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        result_filter: str | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch zone touch events."""
        ...

    def get_bounce_events(
        self,
        pair: str,
        interval: int,
        *,
        min_prior_touches: int = 2,
        dedup_hours: int = 0,
        start: datetime | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Deduped bounce events with per-zone LAG window function."""
        ...

    def get_bounce_references(
        self,
        *,
        pair: str | None = None,
        regime: str | None = None,
        min_dt: datetime | None = None,
    ) -> list[dict]:
        """Rows from bounce_reference with optional filters."""
        ...

    # -----------------------------------------------------------------
    # D-Limit indicators + consolidation
    # -----------------------------------------------------------------

    def get_dlimit(
        self,
        pair: str,
        interval: int = 60,
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch D-Limit indicator values from year-partitioned tables."""
        ...

    def get_dlimit_latest(
        self,
        pair: str,
        interval: int = 60,
        *,
        before: datetime | None = None,
    ) -> dict | None:
        """Fetch most recent D-Limit row."""
        ...

    def get_consolidation(
        self,
        pair: str,
        interval: int = 60,
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch Module 7 consolidation signals."""
        ...

    # -----------------------------------------------------------------
    # Orderbook indicators
    # -----------------------------------------------------------------

    def get_orderbook(
        self,
        pair: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Fetch orderbook depth metrics."""
        ...

    def get_orderbook_events_summary(
        self,
        pair: str,
        *,
        lookback_minutes: int = 15,
        at: datetime | None = None,
    ) -> list[dict]:
        """Grouped orderbook_events counts over the lookback window."""
        ...

    # -----------------------------------------------------------------
    # Risk gate + shadow outcomes (IP because risk gate is calibrated
    # against IP regime/R:R distributions)
    # -----------------------------------------------------------------

    def write_risk_gate(self, data: dict) -> int:
        """Insert a risk gate decision."""
        ...

    def update_risk_gate_outcome(self, data: dict) -> int:
        """Update outcome on an existing risk gate decision."""
        ...

    def get_pending_shadow(self, *, limit: int = 100) -> list[dict]:
        """Fetch risk gate decisions needing shadow outcome evaluation."""
        ...

    def update_shadow_outcome(self, data: dict) -> int:
        """Update shadow outcome fields on a risk gate decision."""
        ...

    # -----------------------------------------------------------------
    # Trade theses + scorer outcomes (IP lifecycle audit)
    # -----------------------------------------------------------------

    def write_thesis(self, data: dict) -> int:
        """Insert a trade thesis."""
        ...

    def update_thesis_outcome(self, data: dict) -> int:
        """Update exit/outcome fields on a thesis."""
        ...

    def write_scorer_outcome(self, data: dict) -> int:
        """Insert a scorer outcome record."""
        ...

    # -----------------------------------------------------------------
    # Tumbler scenes + trajectories (IP observation)
    # -----------------------------------------------------------------

    def write_scene(self, data: dict) -> int:
        """Upsert a tumbler scene."""
        ...

    def get_trajectory_templates(self, pair: str) -> list[dict]:
        """Fetch trajectory health templates for a pair."""
        ...

    def get_trajectory_modifiers(self, pair: str) -> list[dict]:
        """Fetch trajectory zone modifiers for a pair."""
        ...

    def write_trajectory(self, data: dict) -> int:
        """Insert a trade trajectory record."""
        ...

    # -----------------------------------------------------------------
    # Calibration inputs (#338)
    # -----------------------------------------------------------------

    def get_cooldown_calibration(
        self,
        pair: str,
        *,
        interval: int = 60,
        lookback_months: int = 12,
    ) -> list[dict]:
        """Break → next-bounce recovery events for CooldownCalibrator.

        Returns rows with keys: pair, break_time, break_price, price_level,
        recovery_hours, depth_pct, capture_rate.
        """
        ...


# =====================================================================
# Combined interface — subscription tier implements both
# =====================================================================


@runtime_checkable
class DataProvider(FrameworkDataProvider, IPDataProvider, Protocol):
    """Combined framework + IP data access.

    Satisfied by the subscription-tier ApiDataProvider. Free-tier Spirit
    will only depend on FrameworkDataProvider; code that uses this union
    type is implicitly subscription-only.
    """
    ...


# =====================================================================
# Singleton factory
# =====================================================================


_provider: DataProvider | None = None


def get_data_provider() -> DataProvider:
    """Return the singleton DataProvider instance (api-mode only).

    Spirit is api-driven; all data access goes through the gateway.
    """
    global _provider
    if _provider is not None:
        return _provider

    from spirit.utils.api_data_provider import ApiDataProvider
    from spirit.utils.config_loader import get_config

    base_url = get_config("SPIRIT_API_URL", "http://10.0.0.4:8000/v1")
    api_key = get_config("SPIRIT_API_KEY", "")
    if not api_key:
        import os
        api_key = os.environ.get("SPIRIT_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "SPIRIT_API_KEY must be set (env var or spirit.yaml). "
            "Run `python3 -m spirit.setup` to configure."
        )
    _provider = ApiDataProvider(base_url=base_url, api_key=api_key)
    logger.info(f"DataProvider: api → {base_url}")

    trace_path = get_config("SPIRIT_DATA_PROVIDER_TRACE", "")
    if not trace_path:
        import os as _os
        trace_path = _os.environ.get("SPIRIT_DATA_PROVIDER_TRACE", "")
    if trace_path:
        from spirit.utils.coverage_recorder import CountingDataProvider

        _provider = CountingDataProvider(_provider)
        logger.info(f"DataProvider: counting wrapper enabled → {trace_path}")

    return _provider


def reset_data_provider() -> None:
    """Reset the singleton (for testing only)."""
    global _provider
    _provider = None
