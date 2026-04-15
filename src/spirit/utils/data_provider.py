"""
DataProvider — Abstract data access for Spirit runtime.

Phase C of the 3-tier architecture (#277). Allows Spirit to run in two modes:
  - 'pg': Direct PostgreSQL via execute_query() (default, same as before)
  - 'api': HTTP calls to the API gateway at api.tradebot.live

Usage:
    from spirit.utils.data_provider import get_data_provider
    dp = get_data_provider()
    rows = dp.get_ohlc(pair='XBTUSD', interval=60, limit=100)

The active provider is determined by SPIRIT_DATA_PROVIDER config key.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from spirit.logger import get_logger

logger = get_logger("data_provider")


@runtime_checkable
class DataProvider(Protocol):
    """Abstract interface for all Spirit data access (read + write).

    Implementations:
      - PgDataProvider: Direct PostgreSQL (src/spirit/utils/pg_data_provider.py)
      - ApiDataProvider: HTTP to API gateway (src/spirit/utils/api_data_provider.py)
    """

    # =================================================================
    # OHLC
    # =================================================================

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

    # =================================================================
    # Zones
    # =================================================================

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
        """Deduped bounce events with per-zone LAG window function.

        Returns list of dicts with keys: zone_id, touch_time, touch_price,
        approach_direction, acted_as, price_level. Used by zone_bounce
        strategy's entry-path cache — see #287.
        """
        ...

    # =================================================================
    # D-Limit Indicators
    # =================================================================

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
        """Fetch most recent D-Limit row. Convenience: limit=1, order desc."""
        ...

    # =================================================================
    # Consolidation Signals
    # =================================================================

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
        """Fetch Module 7 consolidation signals from year-partitioned tables."""
        ...

    # =================================================================
    # State (spirit_state key-value store)
    # =================================================================

    def get_state(self, key: str) -> Any:
        """Fetch a single value from spirit_state by exact key.
        Returns the JSONB value (deserialized), or None if not found.
        """
        ...

    def put_state(self, key: str, value: Any) -> None:
        """Upsert a key-value pair into spirit_state."""
        ...

    def ensure_table(self, table_name: str, create_sql: str) -> bool:
        """Ensure a table exists. PG: runs DDL if missing. API: no-op (True).
        Returns True if table exists/was created.
        """
        ...

    # =================================================================
    # Performance
    # =================================================================

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

    # =================================================================
    # Risk Gate Decisions
    # =================================================================

    def write_risk_gate(self, data: dict) -> int:
        """Insert a risk gate decision. Returns rows affected."""
        ...

    def update_risk_gate_outcome(self, data: dict) -> int:
        """Update outcome on an existing risk gate decision. Returns rows affected."""
        ...

    def get_pending_shadow(self, *, limit: int = 100) -> list[dict]:
        """Fetch risk gate decisions needing shadow outcome evaluation."""
        ...

    def update_shadow_outcome(self, data: dict) -> int:
        """Update shadow outcome fields on a risk gate decision."""
        ...

    # =================================================================
    # Trade Theses
    # =================================================================

    def write_thesis(self, data: dict) -> int:
        """Insert a trade thesis. Returns rows affected."""
        ...

    def update_thesis_outcome(self, data: dict) -> int:
        """Update exit/outcome fields on a thesis. Returns rows affected."""
        ...

    # =================================================================
    # Scorer Outcomes
    # =================================================================

    def write_scorer_outcome(self, data: dict) -> int:
        """Insert a scorer outcome record. Returns rows affected."""
        ...

    # =================================================================
    # Heartbeats
    # =================================================================

    def write_heartbeat(
        self,
        daemon_id: str,
        *,
        instance: str,
        status: str = "ok",
        metadata: dict | None = None,
        run_id: str = "live",
    ) -> int:
        """Upsert a daemon heartbeat. Returns rows affected.

        instance is REQUIRED so pg-mode and api-mode land under the same
        row per (daemon_id, instance). In api-mode the gateway overrides
        from the API key; we still send the body field for protocol
        consistency.
        """
        ...

    # =================================================================
    # Tumbler Scenes
    # =================================================================

    def write_scene(self, data: dict) -> int:
        """Upsert a tumbler scene. Returns rows affected."""
        ...

    # =================================================================
    # Trajectories
    # =================================================================

    def get_trajectory_templates(self, pair: str) -> list[dict]:
        """Fetch trajectory health templates for a pair."""
        ...

    def get_trajectory_modifiers(self, pair: str) -> list[dict]:
        """Fetch trajectory zone modifiers for a pair."""
        ...

    def write_trajectory(self, data: dict) -> int:
        """Insert a trade trajectory record. Returns rows affected."""
        ...

    # =================================================================
    # Pairs
    # =================================================================

    def get_pairs(self, instance: str | None = None) -> list[dict]:
        """Fetch active pairs from pair registry."""
        ...

    # =================================================================
    # Orderbook
    # =================================================================

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
        """Grouped orderbook_events counts over the lookback window.

        Returns list of dicts with keys: event_type, side, count. The
        caller aggregates event_type→summary field mapping — we keep the
        provider a thin data-access layer.
        """
        ...


# =====================================================================
# Singleton factory
# =====================================================================

_provider: DataProvider | None = None


def get_data_provider() -> DataProvider:
    """Return the singleton DataProvider instance.

    Created on first call based on SPIRIT_DATA_PROVIDER config:
      - 'pg' (default): PgDataProvider (direct PostgreSQL)
      - 'api': ApiDataProvider (HTTP to API gateway)
    """
    global _provider
    if _provider is not None:
        return _provider

    from spirit.utils.config_loader import get_config

    mode = get_config("SPIRIT_DATA_PROVIDER", "pg")

    if mode == "api":
        from spirit.utils.api_data_provider import ApiDataProvider

        base_url = get_config("SPIRIT_API_URL", "http://10.0.0.4:8000/v1")
        api_key = get_config("SPIRIT_API_KEY", "")
        if not api_key:
            import os
            api_key = os.environ.get("SPIRIT_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "SPIRIT_DATA_PROVIDER=api requires SPIRIT_API_KEY to be set "
                "(env var or spirit.yaml)"
            )
        _provider = ApiDataProvider(base_url=base_url, api_key=api_key)
        logger.info(f"DataProvider: api → {base_url}")
    else:
        from spirit.utils.pg_data_provider import PgDataProvider

        _provider = PgDataProvider()
        logger.info("DataProvider: pg (direct PostgreSQL)")

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
